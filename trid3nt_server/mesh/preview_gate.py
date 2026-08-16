"""Shared mesh preview/approve gate.

The mesh preview/approve gate defaults ON for tin
paradigms (TELEMAC precedent) and is per-run-mode (USER-GATED) for regular
grids. This component makes that ONE decision + envelope for every engine, so
TELEMAC, SWMM, SFINCS, SWAN and MODFLOW share a single approve-mesh gate instead
of each re-deriving it.

It rides the EXISTING pause/resume spine -- no new WS event, no new confirmation
envelope, no registry growth (mesh census + #154 doctrine): the gate emits a
:class:`~trid3nt_contracts.payload_warning.PayloadWarningEnvelopePayload` carrying
a :class:`~trid3nt_contracts.payload_warning.GranularitySuggestion` (mesh stats)
exactly like the TELEMAC mesh gate does, so ``server.py``'s
``_PENDING_CONFIRMATIONS`` block-and-wait + the ``tool-payload-confirmation``
resume path handle it unchanged, and the user's approve/override rides back on
``decision`` + ``revised_args``. The mesh WIREFRAME preview layer is published
separately by the caller (a ``LayerURI`` role="input") -- this component owns the
firing DECISION + the gate STATS envelope.

Two-mode run doctrine (mesh census "AUTO = proceed + labeled stats; USER-GATED =
preview wireframe on canvas, approve, then solve"):

  * ``tin`` paradigm (TELEMAC, HEC-RAS): the gate defaults ON -- an unstructured
    mesh is expensive to get wrong, so the user always sees + approves it.
  * ``regular_grid`` / ``raster_cell_graph`` / ``amr_patches``: the gate is OFF
    in AUTO mode and ON only in USER-GATED mode. The run-mode lever is a LATER
    wave (the input-gate feature); this component takes ``mode`` as a PARAMETER
    whose ``None`` default resolves to the paradigm default (tin=ON/regular=OFF),
    so the later wave only has to thread ``mode="user_gated"`` from the lever --
    no change here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from trid3nt_contracts import new_ulid
from trid3nt_contracts.payload_warning import (
    GranularitySuggestion,
    PayloadWarningEnvelopePayload,
)

__all__ = [
    "MeshParadigm",
    "GateMode",
    "MeshGateStats",
    "default_gate_mode",
    "mesh_gate_should_fire",
    "build_mesh_gate_envelope",
]

#: The mesh paradigms the layer generates (mesh census stage-2 vocabulary).
MeshParadigm = Literal["tin", "regular_grid", "raster_cell_graph", "amr_patches"]

#: Run-mode lever. AUTO proceeds with labelled stats; USER_GATED pauses for the
#: preview/approve. ``None`` at a call site resolves to :func:`default_gate_mode`.
GateMode = Literal["auto", "user_gated"]

#: Paradigms whose gate defaults ON (the signed tin default).
_DEFAULT_ON_PARADIGMS = frozenset({"tin"})


def default_gate_mode(paradigm: str) -> GateMode:
    """The signed per-paradigm default: tin -> ``user_gated``, else ``auto``."""
    return "user_gated" if paradigm in _DEFAULT_ON_PARADIGMS else "auto"


def mesh_gate_should_fire(paradigm: str, mode: GateMode | None = None) -> bool:
    """Whether the preview/approve gate fires for ``paradigm`` under ``mode``.

    ``mode`` is the SAME run-mode lever as the in-tool input-review gate: an
    explicit ``"user_gated"`` (a per-run ``input_mode`` threaded here) fires the
    gate for EVERY paradigm, including regular grids. ``mode=None`` resolves to
    :func:`default_gate_mode` (tin=ON / regular=OFF); a regular grid then ALSO
    consults the session-level lever (``TRID3NT_INPUT_GATE_MODE``), so a session
    that opted into user_gated flips regular-grid meshes ON too. A tin run always
    gates (its own signed ON default).
    """
    if mode is not None:
        return mode == "user_gated"
    resolved = default_gate_mode(paradigm)
    if resolved != "user_gated":
        # Regular / raster / amr: the shared session lever can turn the gate ON.
        from trid3nt_server.gates.input_review import resolve_input_gate_mode

        resolved = resolve_input_gate_mode(None)
    return resolved == "user_gated"


@dataclass
class MeshGateStats:
    """The mesh statistics + preview handle a gate card renders.

    Fields:
        paradigm: the mesh paradigm (drives the default gate mode).
        engine / resolution_param: the EXISTING contract enum values the gate
            envelope carries (``GranularitySuggestion`` is a closed Literal, so a
            new engine needs a justified contract change -- not this wave).
        resolution_m: the mesh's target cell/edge size in metres.
        cells: cell (or element) count of the previewed mesh.
        nodes: node count (0 when a paradigm has no distinct node count).
        preview_uri: s3:// URI of the published wireframe preview layer (the
            caller publishes the LayerURI; the gate records the handle).
        resolution_choices: the selectable resolution ladder (metres).
        estimated_solve_seconds / compute_class / vcpus / cell_cap: cost readout.
        reason: short human rationale for the readout.
    """

    paradigm: MeshParadigm
    engine: Literal["swmm", "sfincs", "dem", "topobathy", "landcover", "telemac"]
    resolution_param: Literal[
        "target_resolution_m", "grid_resolution_m", "resolution_m", "mesh_resolution_m"
    ]
    resolution_m: float
    cells: int
    nodes: int = 0
    preview_uri: str | None = None
    resolution_choices: list[float] = field(default_factory=list)
    estimated_solve_seconds: float = 0.0
    compute_class: str = "local"
    vcpus: int = 1
    cell_cap: int = 0
    reason: str = ""


def build_mesh_gate_envelope(
    stats: MeshGateStats,
    *,
    tool_name: str,
    tool_args: dict[str, Any] | None = None,
    warning_id: str | None = None,
    ttl_seconds: int = 300,
) -> PayloadWarningEnvelopePayload:
    """Build the mesh preview/approve gate envelope (rides the existing spine).

    Emits a ``tool-payload-warning`` carrying a ``GranularitySuggestion`` sourced
    from the mesh ``stats`` -- the SAME shape the TELEMAC mesh gate emits, so
    ``server.py`` pauses on it via ``_PENDING_CONFIRMATIONS`` and resumes on the
    user's ``tool-payload-confirmation`` (approve -> proceed; ``narrow_scope`` +
    ``revised_args[resolution_param]`` -> re-mesh at the chosen rung). ``options``
    always includes ``proceed`` (a mesh gate is an approve, not a hard cap).

    The ``preview_uri`` is threaded into ``tool_args`` so the card can point at
    the already-published wireframe layer; ``estimated_mb`` is 0.0 (a mesh gate
    is an approve, not a payload-size warning) with ``threshold_mb`` 0.0.
    """
    args = dict(tool_args or {})
    if stats.preview_uri:
        args.setdefault("mesh_preview_uri", stats.preview_uri)
    args.setdefault(stats.resolution_param, stats.resolution_m)

    granularity = GranularitySuggestion(
        engine=stats.engine,
        resolution_param=stats.resolution_param,
        suggested_resolution_m=float(stats.resolution_m),
        resolution_choices=list(stats.resolution_choices),
        estimated_active_cells=int(stats.cells),
        estimated_solve_seconds=float(stats.estimated_solve_seconds),
        vcpus=int(stats.vcpus),
        compute_class=stats.compute_class,
        cell_cap=int(stats.cell_cap) if stats.cell_cap > 0 else max(1, int(stats.cells)),
        coarsened=False,
        reason=stats.reason
        or (
            f"{stats.paradigm} mesh: {int(stats.cells)} cells"
            + (f" / {int(stats.nodes)} nodes" if stats.nodes else "")
            + f" at {stats.resolution_m:g} m -- review + approve before solving."
        ),
        spot_label=None,
    )

    recommendation = (
        f"Preview the {stats.paradigm} mesh ({int(stats.cells)} cells"
        + (f", {int(stats.nodes)} nodes" if stats.nodes else "")
        + f", {stats.resolution_m:g} m) on the map and approve to solve, or pick a "
        "coarser/finer resolution."
    )

    return PayloadWarningEnvelopePayload(
        warning_id=warning_id or new_ulid(),
        tool_name=tool_name,
        tool_args=args,
        estimated_mb=0.0,
        threshold_mb=0.0,  # a mesh gate is an approve, not a payload-size warning
        recommendation=recommendation[:512],
        options=["proceed", "cancel", "narrow_scope"],
        ttl_seconds=ttl_seconds,
        granularity=granularity,
    )
