"""Declarative per-engine OUTPUT-QUANTITY spec + the FieldResult union.

STEP 2 of the engine-coverage-levers refactor (additive type system; DEFAULT-OFF
so every deck remains byte-identical). The audit's highest-leverage finding is the
"generic output-quantity publisher": every engine computes far more than it
publishes, and the postprocess COG/timeseries/scalar plumbing is near-identical.
This module is the type substrate for that publisher - a declarative
``OutputQuantitySpec`` per engine (quantity -> reader -> COG / timeseries / scalar
emitter) so adding a published field becomes a one-line registration, not a
bespoke postprocess.

PLACEMENT DECISION (read this - it is deliberate).

The kickoff requires the SPEC to be importable by BOTH the agent AND (eventually)
the Batch worker, mirroring the ``manifest.py`` (worker plain dict) /
``publish_manifest.py`` (agent pydantic mirror) two-definitions-one-schema_version
precedent. The hard constraint is the DEPLOY BOUNDARY:

  - the AGENT image ships ONLY ``grace2_agent`` + ``grace2_contracts`` (its
    pyproject deps); it does NOT ship ``services/workers`` (confirmed in
    ``grace2_agent/qgis_proxy.py``: "this lives in the agent package - not
    services/workers/ - because the agent must import it at runtime and
    services/workers/ is not on the agent's import path").
  - the WORKER images ship ``services/workers/**`` (their CodeBuild context) and
    do NOT ship ``packages/contracts``.

So NO single existing location is on BOTH import paths. The manifest precedent
resolves this with TWO definitions gated on ONE ``schema_version``. We follow it:

  * THIS module (agent-side) holds the SPEC as PLAIN FROZEN DATACLASSES + an
    ``OUTPUT_REGISTRY_SCHEMA_VERSION``. It lives in ``grace2_contracts`` because
    that is the package the AGENT already imports, and it uses ONLY the stdlib +
    typing (NO pydantic, NO rasterio, NO engine deps) so it is trivially
    MIRRORABLE into a worker plain module verbatim.
  * STEP 4 (deferred, gated) adds the worker MIRROR under
    ``services/workers/_raster_postprocess/output_quantities.py`` gated on the
    SAME ``OUTPUT_REGISTRY_SCHEMA_VERSION`` - the moment the worker executor is
    wired. Until then the worker does not need it (the executor is STEP 4).

Why plain dataclasses, not ``GraceModel``: the spec must be copy-pasteable into a
worker module that cannot import pydantic-heavy ``grace2_contracts``. A frozen
dataclass is the lowest-common-denominator both sides can host identically. The
``reader`` is an OPTIONAL ``Callable`` bound on the consuming side (the agent
executor binds rasterio/engine readers; a worker mirror would bind worker
readers) - the DECLARATIVE half (id, kind, style_preset, units, role, label) is
what travels, the reader is bound where the heavy deps live.

DEFAULT-OFF: the per-engine ``OUTPUT_QUANTITIES`` registry ships as an EMPTY
scaffold (no engine migrated yet - that is STEP 3). ``get_output_registry`` of any
engine returns ``()`` today, so nothing changes until an engine opts in. The
executor (``grace2_agent.workflows.publish_quantities``) is importable + typed +
unit-tested against a FAKE registry now; the per-engine fan-out is STEP 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

__all__ = [
    "OUTPUT_REGISTRY_SCHEMA_VERSION",
    "FieldKind",
    "RasterField",
    "TimeseriesField",
    "ScalarField",
    "FieldResult",
    "OutputQuantitySpec",
    "OUTPUT_QUANTITIES",
    "get_output_registry",
]

#: Bumped whenever the OutputQuantitySpec SHAPE changes incompatibly. A future
#: worker MIRROR module gates on this exact value (the manifest precedent).
OUTPUT_REGISTRY_SCHEMA_VERSION: int = 1


#: The kind of published artifact a quantity produces. Drives the executor's
#: routing: ``raster`` -> cog_io COG, ``timeseries`` -> frames.emit_timeseries,
#: ``scalar`` -> metrics dict.
FieldKind = Literal["raster", "timeseries", "scalar"]


# --------------------------------------------------------------------------- #
# FieldResult union - what a spec.reader returns (the executor routes on type).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RasterField:
    """A single 2D field to publish as ONE COG layer.

    ``grid`` is a 2D array (the reader's responsibility to orient + mask);
    ``src_crs`` / ``src_transform`` georegister it; ``reproject`` selects the
    cog_io path (already-4326 direct-write vs projected->4326 warp). ``mask`` is
    the optional per-cell mask (declared per quantity, e.g. mask-below-floor).
    ``metrics`` carries the narration scalars the layer row needs.
    """

    grid: Any
    src_crs: str
    src_transform: Any
    reproject: bool = False
    mask: Callable[[Any], Any] | None = None
    crs_roundtrip_guard: bool = False
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TimeseriesField:
    """A time-varying field to publish as a PEAK COG + N animation-frame COGs.

    ``n_steps`` is the raw step count (the executor subsamples to
    ``frames.MAX_FLOOD_FRAMES``); ``read_step(raw_index) -> RasterField`` reads
    one frame's grid on demand (so the reader never materializes all frames at
    once); ``peak`` is the representative PEAK ``RasterField`` (always published
    as ``layers[0]``). ``quantity_label`` is the web token base (e.g.
    ``"Flood depth"`` -> "Peak flood depth" / "Flood depth step N").
    """

    n_steps: int
    read_step: Callable[[int], RasterField]
    peak: RasterField
    quantity_label: str = "Flood depth"


@dataclass(frozen=True)
class ScalarField:
    """A scalar (or small dict of scalars) routed to the run metrics, no layer.

    ``values`` is merged into the executor's metrics dict. Used for quantities a
    run computes but does not rasterize (e.g. a basin-total, a convergence stat).
    """

    values: dict[str, Any]


#: What a ``OutputQuantitySpec.reader`` returns.
FieldResult = RasterField | TimeseriesField | ScalarField


# --------------------------------------------------------------------------- #
# OutputQuantitySpec - one declarative published quantity per engine.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OutputQuantitySpec:
    """One published output quantity for an engine (the declarative half).

    Fields:
        quantity_id: stable id (the layer-id STEM + the registry key), e.g.
            ``"flood-depth"`` / ``"flood-velocity"`` / ``"plume-concentration"``.
        kind: ``raster`` | ``timeseries`` | ``scalar`` (routes the executor).
        name: the human + web-grouping LayerURI name (e.g. "Peak flood depth").
            For a ``timeseries`` the executor derives the peak/frame names from
            the ``TimeseriesField.quantity_label`` instead; ``name`` is the peak.
        style_preset: the publish_layer / TiTiler style-preset KEY.
        units: the layer units string (e.g. "meters", "mg/L", "g").
        role: the LayerURI role (peak = "primary", frames = "context").
        reader: OPTIONAL callable ``(ctx) -> FieldResult`` bound on the consuming
            side (the agent executor binds rasterio/engine readers). The
            DECLARATIVE fields above travel between agent + worker mirror; the
            reader is bound where the heavy deps live. ``None`` in a pure scaffold
            entry (the executor skips a spec with no reader, honestly logging it).
        default_on: when False (the additive default), the quantity is OFF until
            an engine opts it in (DEFAULT-OFF guarantee - decks stay byte-
            identical). The executor skips an ``default_on=False`` spec unless the
            run args explicitly enable it.
        doc: one-line human description (catalog / narration).
    """

    quantity_id: str
    kind: FieldKind
    name: str
    style_preset: str
    units: str = ""
    role: str = "primary"
    reader: Callable[..., FieldResult] | None = None
    default_on: bool = False
    doc: str = ""


# --------------------------------------------------------------------------- #
# Per-engine registry (STEP 3: MODFLOW / Landlab / OpenQuake / SWMM migrated).
# --------------------------------------------------------------------------- #
#: engine -> ordered tuple of OutputQuantitySpec. The DECLARATIVE half lives
#: here (id / kind / style_preset / units / role / label); the ``reader`` is
#: bound on the AGENT side (this module ships in ``grace2_contracts`` which must
#: stay pydantic/rasterio-free, so a reader that needs rasterio/flopy/pyswmm
#: cannot live here). Each engine's agent-side ``*_quantity_readers`` module
#: clones these scaffold rows with a bound reader (``dataclasses.replace``) and
#: hands the runtime specs to ``publish_quantities``.
#:
#: DEFAULT-OFF discipline: ``default_on`` gates which quantities the executor
#: publishes. The EXISTING headline quantity per engine (plume / susceptibility /
#: hazard-map / invert-depth) keeps coming from the engine's BYTE-IDENTICAL old
#: postprocess path, so it is declared with ``default_on=False`` here purely as
#: provenance (the executor skips it). The NEW quantities are ``default_on=True``
#: (they are additive layers the old path never produced) and ride the executor.
#:
#: SFINCS / GeoClaw / SWAN stay EMPTY (STEP 0 / STEP 4, out of scope here).
OUTPUT_QUANTITIES: dict[str, tuple[OutputQuantitySpec, ...]] = {
    "sfincs": (),
    "swmm": (
        # EXISTING headline (peak invert depth) -- old postprocess path owns it.
        OutputQuantitySpec(
            quantity_id="swmm-depth",
            kind="timeseries",
            name="Peak flood depth",
            style_preset="continuous_flood_depth",
            units="meters",
            role="primary",
            default_on=False,
            doc="Per-node INVERT_DEPTH scattered to the mesh (existing path).",
        ),
        # NEW: node FLOODING_LOSSES (surface flooding rate) -> per-cell raster.
        OutputQuantitySpec(
            quantity_id="swmm-flooding-losses",
            kind="raster",
            name="Node flooding rate",
            style_preset="continuous_flooding_losses",
            units="m^3/s",
            role="context",
            default_on=True,
            doc="Peak per-node FLOODING_LOSSES (surface surcharge rate).",
        ),
        # NEW: node PONDED_VOLUME -> per-cell raster.
        OutputQuantitySpec(
            quantity_id="swmm-ponded-volume",
            kind="raster",
            name="Ponded volume",
            style_preset="continuous_ponded_volume",
            units="m^3",
            role="context",
            default_on=True,
            doc="Peak per-node PONDED_VOLUME (standing water held at a node).",
        ),
        # NEW: conduit FLOW_RATE (signed) -> per-cell raster (link->downstream cell).
        OutputQuantitySpec(
            quantity_id="swmm-conduit-flow",
            kind="raster",
            name="Conduit flow",
            style_preset="diverging_conduit_flow",
            units="m^3/s",
            role="context",
            default_on=True,
            doc="Peak per-conduit FLOW_RATE scattered to the link's cell.",
        ),
        # NEW: conduit FLOW_VELOCITY -> per-cell raster.
        OutputQuantitySpec(
            quantity_id="swmm-conduit-velocity",
            kind="raster",
            name="Conduit velocity",
            style_preset="continuous_conduit_velocity",
            units="m/s",
            role="context",
            default_on=True,
            doc="Peak per-conduit FLOW_VELOCITY scattered to the link's cell.",
        ),
    ),
    "modflow": (
        # EXISTING headline (final-step plume) -- old postprocess path owns it.
        OutputQuantitySpec(
            quantity_id="plume-concentration",
            kind="raster",
            name="Contaminant Plume (peak concentration)",
            style_preset="continuous_plume_concentration",
            units="mg/L",
            role="primary",
            default_on=False,
            doc="Final-step max-over-layers concentration (existing path).",
        ),
        # NEW: ALL saved transport steps -> peak + animation frames.
        OutputQuantitySpec(
            quantity_id="plume-concentration-ts",
            kind="timeseries",
            name="Plume concentration",
            style_preset="continuous_plume_concentration",
            units="mg/L",
            role="primary",
            default_on=True,
            doc="All saved UCN transport steps as a concentration animation.",
        ),
        # NEW: GWF head / water table (one more .hds read).
        OutputQuantitySpec(
            quantity_id="water-table",
            kind="raster",
            name="Water table (head)",
            style_preset="continuous_head_m",
            units="meters",
            role="context",
            default_on=True,
            doc="Final-step max-over-layers GWF head from the saved .hds.",
        ),
        # EXISTING seepage -- old postprocess path owns it (provenance only).
        OutputQuantitySpec(
            quantity_id="river-seepage",
            kind="raster",
            name="River Seepage (gaining / losing reach)",
            style_preset="diverging_river_seepage",
            units="m^3/day",
            role="primary",
            default_on=False,
            doc="Per-cell signed RIV exchange flux (existing path).",
        ),
        # NEW (sprint-18 Wave-1): sustainable_yield drawdown -> head-decline COG.
        OutputQuantitySpec(
            quantity_id="drawdown",
            kind="raster",
            name="Pumping drawdown (head decline)",
            style_preset="continuous_drawdown_m",
            units="meters",
            role="primary",
            default_on=True,
            doc="Pre-pumping minus pumped GWF head (cone of depression).",
        ),
        # NEW (sprint-18 Wave-1): mine_dewatering DRN outflow -> dewatering-rate COG.
        OutputQuantitySpec(
            quantity_id="dewatering-rate",
            kind="raster",
            name="Mine dewatering rate",
            style_preset="continuous_dewatering_rate",
            units="m^3/day",
            role="primary",
            default_on=True,
            doc="Per-cell DRN outflow over the pit footprint (pump-to-dewater rate).",
        ),
        # NEW (sprint-18 Wave-1): regional_water_budget zonal partition -> scalars.
        OutputQuantitySpec(
            quantity_id="budget-partition",
            kind="scalar",
            name="Regional water budget partition",
            style_preset="continuous_head_m",
            units="m^3/day",
            role="context",
            default_on=True,
            doc="Per-zone cell-budget flow partition (CHD/RIV/WEL/storage) -> metrics.",
        ),
        # NEW (sprint-18 Wave-2): MAR groundwater mounding -> head-rise COG.
        OutputQuantitySpec(
            quantity_id="mounding",
            kind="raster",
            name="Recharge mounding (head rise)",
            style_preset="continuous_mounding_m",
            units="meters",
            role="primary",
            default_on=True,
            doc="Recharged minus pre-recharge GWF head under the MAR basin (mound).",
        ),
        # NEW (sprint-18 Wave-2): ASR recovery-efficiency -> scalar/metrics + chart.
        OutputQuantitySpec(
            quantity_id="recovery-efficiency",
            kind="scalar",
            name="ASR recovery efficiency",
            style_preset="continuous_head_m",
            units="fraction",
            role="context",
            default_on=True,
            doc="ASR recovered/injected fraction + inject/recover head sawtooth -> metrics + chart.",
        ),
        # NEW (sprint-18 Wave-2): wetland seasonal head-range -> hydroperiod COG.
        OutputQuantitySpec(
            quantity_id="hydroperiod",
            kind="raster",
            name="Wetland hydroperiod (seasonal head range)",
            style_preset="continuous_hydroperiod_m",
            units="meters",
            role="primary",
            default_on=True,
            doc="Seasonal water-table swing (max-min head) under the wetland footprint.",
        ),
    ),
    "geoclaw": (),
    "landlab": (
        # EXISTING headline (the analysis-selected primary field).
        OutputQuantitySpec(
            quantity_id="landlab-susceptibility",
            kind="raster",
            name="Landslide susceptibility",
            style_preset="continuous_landslide_susceptibility",
            units="probability",
            role="primary",
            default_on=False,
            doc="Probability-of-failure / peak-depth primary (existing path).",
        ),
        # NEW: discarded grids the component chain already computes (worker
        # emits each as its own COG; the agent reprojects + publishes them).
        OutputQuantitySpec(
            quantity_id="landlab-drainage-area",
            kind="raster",
            name="Drainage area",
            style_preset="continuous_drainage_area",
            units="m^2",
            role="context",
            default_on=True,
            doc="Upstream contributing drainage area (FlowAccumulator).",
        ),
        OutputQuantitySpec(
            quantity_id="landlab-slope",
            kind="raster",
            name="Topographic slope",
            style_preset="continuous_slope",
            units="m/m",
            role="context",
            default_on=True,
            doc="Topographic steepest slope (rise/run).",
        ),
        OutputQuantitySpec(
            quantity_id="landlab-relative-wetness",
            kind="raster",
            name="Relative wetness",
            style_preset="continuous_relative_wetness",
            units="fraction",
            role="context",
            default_on=True,
            doc="Soil mean relative wetness (LandslideProbability).",
        ),
        OutputQuantitySpec(
            quantity_id="landlab-discharge",
            kind="raster",
            name="Surface-water discharge",
            style_preset="continuous_discharge_m3s",
            units="m^3/s",
            role="context",
            default_on=True,
            doc="Surface-water discharge (overland routing).",
        ),
        OutputQuantitySpec(
            quantity_id="landlab-factor-of-safety",
            kind="raster",
            name="Factor of safety",
            style_preset="continuous_factor_of_safety",
            units="dimensionless",
            role="context",
            default_on=True,
            doc="Deterministic infinite-slope factor of safety (FoS<1 fails).",
        ),
    ),
    "openquake": (
        # EXISTING headline hazard MAP -- old postprocess path owns the raster.
        OutputQuantitySpec(
            quantity_id="seismic-hazard",
            kind="raster",
            name="Seismic hazard map",
            style_preset="continuous_seismic_pga",
            units="g",
            role="primary",
            default_on=False,
            doc="Per-site hazard value rasterized (existing path).",
        ),
        # NEW: hazard CURVES (PoE vs IML) -> scalars/metrics + chart (no raster).
        OutputQuantitySpec(
            quantity_id="hazard-curves",
            kind="scalar",
            name="Hazard curves",
            style_preset="continuous_seismic_pga",
            units="g",
            role="context",
            default_on=True,
            doc="Mean hazard curve(s) (PoE vs IML) -> metrics + chart path.",
        ),
        # NEW: uniform hazard spectrum (SA vs period) -> scalars/metrics + chart.
        OutputQuantitySpec(
            quantity_id="uhs",
            kind="scalar",
            name="Uniform hazard spectrum",
            style_preset="continuous_seismic_pga",
            units="g",
            role="context",
            default_on=True,
            doc="Uniform hazard spectrum (SA vs period) -> metrics + chart.",
        ),
    ),
    "swan": (),
}


def get_output_registry(engine: str) -> tuple[OutputQuantitySpec, ...]:
    """Return the ordered ``OutputQuantitySpec`` tuple for ``engine`` (or ``()``).

    The resolver the STEP-2 executor walks. Unknown engine -> empty tuple (the
    executor publishes nothing, exactly the DEFAULT-OFF behavior). The lookup is
    case-insensitive on the engine token.
    """
    return OUTPUT_QUANTITIES.get(engine.strip().lower(), ())
