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

    kind: Literal["continuous", "classed", "reference", "mesh"]

    #: The ramp name, or explicit stops as ``[[stop_0to1, "#rrggbb"], ...]``.
    colormap: str | list[tuple[float, str]] | None = None
    vmin: float | None = None
    vmax: float | None = None
    units: str | None = None
    #: Where the field stops being drawn; a renderer masks below it rather than painting the ramp's bottom over an absent region.
    floor: float | None = None
    #: The resolved preset as a QGIS ``.qml``, the one record of how the layer is painted; None where the file carries its own colours.
    qml: str | None = None


class ExecutionHandle(ContractModel):
    """A submitted execution, and the CANCELLATION contract.
    The execution identifier is a first-class field, so a cancel terminates by
    it instead of parsing one out of a string. One handle, every backend."""

    schema_version: Literal["v1"] = "v1"

    handle_id: ULIDStr
    run_id: ULIDStr  # the run this execution backs
    solver: str
    #: The partition count, the same number the engine's input file states.
    cores: int = Field(ge=1)

    # The identifier a terminate is issued against, and the definition and region it names.
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

    error_code: str | None = None
    error_message: str | None = None

    cancellation_reason: str | None = None

    # Best-effort instance and timing capture so a completion-time model can be inferred from measurements.
    # None on an in-process run or any describe failure: the capture swallows its exceptions, because a
    # missing measurement must never fail a finished run. Every key optional.
    batch_compute_meta: dict | None = None


class LayerURI(ContractModel):
    """ONE produced output layer, aligned field-for-field with the
    ``load-layer`` map command so it reaches the map untranslated. The producer
    DECLARES a style; the publish path RESOLVES it into ``legend``."""

    layer_id: str  # stable id; flows into the load-layer args
    name: str
    # ``mesh`` is the one format a client stages rather than streams.
    layer_type: Literal["raster", "vector", "mesh"]
    uri: str  # COG / FlatGeobuf / GeoParquet / UGRID netCDF
    #: The declared style row; None is the kind's bare default.
    style: dict[str, Any] | None = None
    #: The physical quantity, as its producer names it: the layer's identity, by which a still, a frame and
    #: an animation of one field share a scale (a title is prose and may be rewritten).
    quantity: str | None = None
    #: Which tracer, counted from 1 in the deck's order; a tracer's name is the run's own, so position is the stable key.
    tracer: int | None = None
    role: Literal["primary", "context", "input"] = "primary"
    #: Where the layer came from, read by a person; ``user`` is a file the user pushed in. Nothing branches on it.
    origin: Literal["user"] | None = None
    units: str | None = None
    # (min_lon, min_lat, max_lon, max_lat) in EPSG:4326; the camera flies to it once the layer is added.
    bbox: tuple[float, float, float, float] | None = None
    legend: LegendKey | None = None  # the resolved style; None until published
    # Every sentence the producer and run state about the layer; a fallback substitution names both sources.
    notes: list[str] = Field(default_factory=list)
    # Physical model inputs, each tagged with where it came from; [] means no provenance declared, not "all real".
    synthetic_inputs: list[SyntheticInput] = Field(default_factory=list)
    # MDAL dataset files a mesh row carries beside its groups, loaded before the declared group is bound; [] for every other row.
    dataset_uris: list[str] = Field(default_factory=list)
    # CRS authority id for a mesh row, whose reader reports an empty CRS; None for raster or vector.
    crs_authid: str | None = None
    # The instant a mesh's dataset times are counted from (a SELAFIN records no origin); None for raster or vector.
    reference_time: str | None = None
    # ISO-8601 UTC window [valid_from, valid_to) of one frame of an ordered sequence, stamped straight onto the map.
    valid_from: str | None = None
    valid_to: str | None = None
    #: The zero elevations are counted from, as the source states it; surfaces share an axis only when it agrees. None is never NAVD88.
    vertical_datum: str | None = None
    #: The shift the source publishes about its own zero: ``datum_offset_m`` added to a value on
    #: ``vertical_datum`` reads it on ``datum_offset_frame``. The two ride together; None is none published.
    datum_offset_m: float | None = None
    datum_offset_frame: str | None = None

    @classmethod
    def published(cls, prefix: str, *, seed: str, **fields: Any) -> "LayerURI":
        """A layer this run produced, its id ``<prefix>-<seed>``; the seed is the caller's because the file carries it too."""
        return cls(layer_id=f"{prefix}-{seed}", **fields)


def layer_seed() -> str:
    """A fresh stem for one produced layer - its id and its file share it."""
    import uuid

    return uuid.uuid4().hex[:8]


# LayerURI subclass result models: a source whose result carries business fields computed after
# serialization names its subclass in source.yaml; ``LAYER_RESULT_MODELS`` is the name -> class table.


class HighWaterMarksLayerURI(LayerURI):
    """A high-water-mark point layer plus its survey-quality envelope."""


    n_marks: int = 0
    event: str | None = None
    #: ``{label: count}`` over surveyor accuracy, mark type and vertical datum.
    quality_breakdown: dict[str, int] = {}
    type_breakdown: dict[str, int] = {}
    datum_summary: dict[str, int] = {}
    #: Water-surface elevation above the stated datum, not a depth above ground, so a comparison never pairs it with a model depth raster.
    observed_quantity: str = "water_surface_elevation"
    caveats: list[str] = []


class FaultSourcesResult(LayerURI):
    """An active-fault trace layer plus the kinematic source records."""


    catalog: str = "gem"
    #: Always equals ``len(faults)``.
    fault_count: int = 0
    faults: list[dict] = []
    source: str = "GEM Global Active Faults (harmonized)"
    #: Always None here: an empty AOI returns a bare record instead of a layer.
    note: str | None = None


class FloodExtentObservationResult(LayerURI):
    """An OBSERVED flood-extent layer plus its observation envelope."""


    product: str = "MCDWD_L3_F3_NRT"
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


    #: The vintage the roughness mapping is validated against; None for a dataset with no such mapping.
    nlcd_vintage_year: int | None = None
    dataset: str = "nlcd_2021"
    source: str = "mrlc-wcs"
    #: The pixel spacing actually DELIVERED, against the dataset's native.
    effective_resolution_m: int = 30
    native_resolution_m: int = 30
    #: True when coarsened above native for a large AOI, with the caveat in the note.
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


    #: The datum read off the tiles that painted, verbatim; orthometric rather than tidal, so it merges with a land DEM unconverted.
    vertical_datum: str = "NAVD88"
    tile_count: int = 0
    #: The tile scheme's own resolution labels; a merge may span tiers.
    resolution_tiers: list[str] = Field(default_factory=list)
    #: Share of the requested AOI the footprints cover; bathymetry-only and on navigable water, so a shore-spanning AOI is partial by construction, not a failure.
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


#: name -> subclass; a name absent from this table is a registration error, not a fallback.
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
