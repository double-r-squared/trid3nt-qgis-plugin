"""Solver-execution shapes (FR-TA-2): ModelSetup, RunResult, ExecutionHandle,
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

from typing import Literal

from pydantic import Field

from .common import GraceModel, ULIDStr, UTCDatetime
from .envelope import TemporalConfig

__all__ = [
    "ComputeClass",
    "ModelSetup",
    "ExecutionHandle",
    "RunResult",
    "LegendClass",
    "LegendKey",
    "LayerURI",
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

    # --- Cancellation seam (FR-CE-2/3, FR-AS-6) ---
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

    ``bbox`` is optional (job-0068): when present the pipeline emitter emits a
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
    layer_type: Literal["raster", "vector"]
    uri: str  # gs://... COG / FlatGeobuf / GeoParquet
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
