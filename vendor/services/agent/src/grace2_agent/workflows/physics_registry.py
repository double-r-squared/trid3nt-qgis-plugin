"""Per-engine advanced-physics override registry + validate/resolve (STEP 2).

The substrate for the audit's "physics-toggle exposure" pattern: every engine
pins its physics to demo-correct defaults and exposes near-nothing. This module
is the declarative, per-engine table of the calibration knobs an engine CAN
accept as ``EngineRunArgsMixin.advanced_physics`` overrides, plus the validator
that turns a user/LLM overrides dict into a typed, range-checked resolved dict.

STEP-2 SCOPE: define the tables + the validate/resolve/delta helpers. They are
NOT wired into any deck builder yet (that is STEP 3); a deck builder that does not
read the resolved dict is byte-identical to today. ``advanced_physics`` defaults
to ``None`` on the mixin, and ``validate_and_resolve_physics(engine, None)``
returns ``{}`` - so nothing changes until an engine opts in.

Registry entry shape (per key):
    {
      "type":  python type the value must coerce to (float | int | bool | str),
      "range": (lo, hi) inclusive numeric bounds, or a tuple of allowed str/bool
               literals, or ``None`` for an unbounded numeric / free value,
      "default": the engine's current pinned value (what the deck uses today when
                 the key is NOT overridden - documents the demo default),
      "deck_target": a human/string pointer to WHERE the value lands in the deck
                     (e.g. "sfincs.inp:advection", "GwtMst.distcoef") so STEP 3
                     wiring is unambiguous,
      "doc": one-line description (catalog / narration / credential card).
    }

These tables intentionally mirror the audit's per-engine physics gaps; they are
the long-tail of full coverage and are LOWER-urgency (defaults are correct for the
demos), so STEP 3 wires them additively + non-breaking.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "PhysicsRegistryError",
    "PHYSICS_REGISTRY",
    "validate_and_resolve_physics",
    "applied_physics_delta",
    "get_engine_physics",
]


class PhysicsRegistryError(ValueError):
    """Raised on an unknown engine, an unknown physics key, or an out-of-range
    / wrong-type override value. Carries ``engine`` + ``key`` for narration."""

    def __init__(
        self, message: str, *, engine: str | None = None, key: str | None = None
    ) -> None:
        super().__init__(message)
        self.engine = engine
        self.key = key


# --------------------------------------------------------------------------- #
# Per-engine physics tables (declarative; NOT wired into decks yet - STEP 3).
# --------------------------------------------------------------------------- #
PHYSICS_REGISTRY: dict[str, dict[str, dict[str, Any]]] = {
    # --- SFINCS (the reference engine; snapwave_inp_overrides already proves the
    # shape). advection / alpha / theta / huthresh / Coriolis / wind-drag. ---
    "sfincs": {
        # NATE 2026-06-26 (doc-grounding, sfincs.readthedocs.io parameters.html):
        # SFINCS advection has ONLY values 0 (SFINCS-LIE local-inertial) or 1
        # (SFINCS-SSWE advection-on). There is NO value 2 ("2nd order") - the
        # solver would silently clamp/reject it (Invariant 7). Range is (0,1).
        "advection": {
            "type": int,
            "range": (0, 1),
            "default": 1,
            "deck_target": "sfincs.inp:advection",
            "doc": "Momentum advection scheme (0=SFINCS-LIE local-inertial, 1=SFINCS-SSWE advection-on); recommended on. No value 2.",
        },
        # NATE 2026-06-26: manual default alpha=0.5, recommended band 0.1-0.75
        # (CFL dt-reduction). Was 0.75 default / (0.1,1.0) range = wrong baseline.
        "alpha": {
            "type": float,
            "range": (0.1, 0.75),
            "default": 0.5,
            "deck_target": "sfincs.inp:alpha",
            "doc": "CFL-based time-step safety factor (manual default 0.5; lower = smaller dt).",
        },
        # NATE 2026-06-26: manual default theta=1.0 (= no smoothing). Was 0.9,
        # which mislabels the engine baseline for the applied_physics_delta
        # narration (would report a false "from 0.9" when the deck baseline is 1.0).
        "theta": {
            "type": float,
            "range": (0.8, 1.0),
            "default": 1.0,
            "deck_target": "sfincs.inp:theta",
            "doc": "Semi-implicit time-integration weighting (manual default 1.0 = no smoothing; range 0.8-1.0).",
        },
        # NATE 2026-06-26: manual default huthresh=0.05 m (parameters.html); a
        # pluvial deck omits the line so the binary's 0.05 default applies. Was
        # 0.01 default / (0.001,0.5) range; corrected to 0.05 / (0.001,0.1).
        "huthresh": {
            "type": float,
            "range": (0.001, 0.1),
            "default": 0.05,
            "deck_target": "sfincs.inp:huthresh",
            "doc": "Wet/dry threshold water depth (m) for momentum (manual default 0.05).",
        },
        # NATE 2026-06-26 (doc-grounding): SFINCS DOES have a `coriolis` keyword
        # (default True), but it is INERT while `latitude`==0.0 on a projected CRS
        # - so `latitude` is the effective lever, not the bool. The old registry
        # entry was a bool `coriolis` -> "sfincs.inp:coriolis" that we'd narrate as
        # flipping Coriolis while actually leaving latitude=0.0 (silent no-effect,
        # Invariant 7). Re-spec to a float `coriolis_latitude` (deg) ->
        # sfincs.inp:latitude (the real activation knob; crsgeo=1 is the alt
        # grid-aware-f path, both real keys per hydromt_sfincs SfincsInput).
        "coriolis_latitude": {
            "type": float,
            "range": (-90.0, 90.0),
            "default": 0.0,
            "deck_target": "sfincs.inp:latitude",
            "doc": (
                "Constant-plane Coriolis latitude (deg) -> sfincs.inp:latitude "
                "(0 = no Coriolis; set the AOI-centre latitude for large-domain surge)."
            ),
        },
        # NATE 2026-06-26: was deck_target "sfincs.inp:cdwnd", but `cdwnd` is the
        # wind-SPEED breakpoint vector [0,28,50] m/s, NOT a drag coefficient. The
        # drag COEFFICIENTS live in `cdval` [0.001,0.0025,0.0015]. A constant-drag
        # override rewrites `cdval` to a flat list [cd,cd,cd] (with cdnrb=3). Fixed
        # deck_target -> "sfincs.inp:cdval"; see _emit_physics_config list semantics.
        "wind_drag": {
            "type": float,
            "range": (0.0, 0.01),
            "default": 0.0,
            "deck_target": "sfincs.inp:cdval",
            "doc": (
                "Constant wind-drag coefficient override written as a flat cdval "
                "curve [cd,cd,cd] with cdnrb=3 (0 = keep SFINCS default formula)."
            ),
        },
    },
    # --- SWAN: whitecapping / breaking-gamma / bottom-friction / quadruplets. ---
    "swan": {
        "whitecapping": {
            "type": str,
            "range": ("komen", "westhuysen", "off"),
            "default": "westhuysen",
            "deck_target": "swan.inp:GEN3",
            "doc": "Whitecapping dissipation formulation.",
        },
        "breaking_gamma": {
            "type": float,
            "range": (0.4, 1.0),
            "default": 0.73,
            "deck_target": "swan.inp:BREAKING",
            "doc": "Depth-induced breaking gamma (Hmax/depth ratio).",
        },
        "friction": {
            "type": str,
            "range": ("jonswap", "collins", "madsen", "off"),
            "default": "jonswap",
            "deck_target": "swan.inp:FRICTION",
            "doc": "Bottom-friction formulation.",
        },
        "friction_coeff": {
            "type": float,
            "range": (0.0, 0.2),
            "default": 0.067,
            "deck_target": "swan.inp:FRICTION:coeff",
            "doc": "Bottom-friction coefficient (units depend on formulation).",
        },
        "quadruplets": {
            "type": bool,
            "range": (True, False),
            "default": True,
            "deck_target": "swan.inp:QUAD",
            "doc": "Enable quadruplet nonlinear wave-wave interactions.",
        },
    },
    # --- SWMM: routing method + numeric tunables (THREADS caps resolution). ---
    "swmm": {
        "routing_method": {
            "type": str,
            "range": ("DYNWAVE", "KINWAVE", "STEADY"),
            "default": "DYNWAVE",
            "deck_target": "swmm.inp:OPTIONS:ROUTING_MODEL",
            "doc": "Flow-routing method (DYNWAVE = full St-Venant).",
        },
        "routing_step_s": {
            "type": float,
            "range": (0.5, 300.0),
            "default": 30.0,
            "deck_target": "swmm.inp:OPTIONS:ROUTING_STEP",
            "doc": "Routing time step (s). Smaller = stabler, slower.",
        },
        "variable_step": {
            "type": float,
            "range": (0.0, 1.0),
            "default": 0.75,
            "deck_target": "swmm.inp:OPTIONS:VARIABLE_STEP",
            "doc": "Adaptive-step safety factor (0 = fixed step).",
        },
        "threads": {
            "type": int,
            "range": (1, 16),
            "default": 1,
            "deck_target": "swmm.inp:OPTIONS:THREADS",
            "doc": "Solver thread count (raises the resolution ceiling).",
        },
    },
    # --- MODFLOW 6 GWT: sorption (Kd) / first-order decay / dispersivity. ---
    "modflow": {
        "sorption_kd": {
            "type": float,
            "range": (0.0, 1000.0),
            "default": 0.0,
            "deck_target": "GwtMst:distcoef",
            "doc": "Linear sorption distribution coefficient Kd (L/kg); 0 = none.",
        },
        "bulk_density": {
            "type": float,
            "range": (500.0, 3000.0),
            "default": 1600.0,
            "deck_target": "GwtMst:bulk_density",
            "doc": "Aquifer bulk density (kg/m^3) for the retardation factor.",
        },
        "decay_rate_per_day": {
            "type": float,
            "range": (0.0, 10.0),
            "default": 0.0,
            "deck_target": "GwtMst:decay",
            "doc": "First-order decay rate (1/day); 0 = conservative tracer.",
        },
        # MF6 GwtMst has a SEPARATE sorbed-phase first-order decay (decay_sorbed)
        # distinct from the aqueous-phase decay above (decay). With sorption on
        # (sorption_kd > 0) a contaminant degrades on the solid phase too; this
        # makes the sorbed decay rate user-controllable so the decay+sorption fix
        # is a real lever (default None = mirror the aqueous decay_rate_per_day at
        # the deck seam, NOT a hard 0 -> byte-identical until set).
        "decay_sorbed_per_day": {
            "type": float,
            "range": (0.0, 10.0),
            "default": None,
            "deck_target": "GwtMst:decay_sorbed",
            "doc": (
                "Sorbed-phase first-order decay rate (1/day); None = mirror the "
                "aqueous decay_rate_per_day (only active when sorption_kd > 0)."
            ),
        },
        "long_dispersivity_m": {
            "type": float,
            "range": (0.1, 1000.0),
            "default": 10.0,
            "deck_target": "GwtDsp:alh",
            "doc": "Longitudinal dispersivity (m); first-order plume-shape knob.",
        },
        "trans_dispersivity_m": {
            "type": float,
            "range": (0.01, 500.0),
            "default": 1.0,
            "deck_target": "GwtDsp:ath1",
            "doc": "Transverse dispersivity (m).",
        },
    },
    # --- GeoClaw: solver order / limiter / CFL / source-splitting. ---
    "geoclaw": {
        "order": {
            "type": int,
            "range": (1, 2),
            "default": 2,
            "deck_target": "setrun:clawdata.order",
            "doc": "Spatial order of the wave-propagation solver.",
        },
        "limiter": {
            "type": str,
            "range": ("none", "minmod", "superbee", "mc", "vanleer"),
            "default": "mc",
            "deck_target": "setrun:clawdata.limiter",
            "doc": "Flux limiter for the high-resolution correction.",
        },
        "cfl_desired": {
            "type": float,
            "range": (0.1, 0.95),
            "default": 0.75,
            "deck_target": "setrun:clawdata.cfl_desired",
            "doc": "Desired CFL number (adaptive time stepping).",
        },
        "source_split": {
            "type": str,
            "range": ("godunov", "strang", "none"),
            "default": "godunov",
            "deck_target": "setrun:clawdata.source_split",
            "doc": "Source-term splitting scheme (friction/Coriolis).",
        },
    },
    # --- OpenQuake: truncation level + magnitude/distance discretization. ---
    "openquake": {
        "truncation_level": {
            "type": float,
            "range": (0.0, 6.0),
            "default": 3.0,
            "deck_target": "job.ini:truncation_level",
            "doc": "GMPE sigma truncation level (std deviations).",
        },
        "rupture_mesh_spacing_km": {
            "type": float,
            "range": (0.5, 20.0),
            "default": 5.0,
            "deck_target": "job.ini:rupture_mesh_spacing",
            "doc": "Rupture-surface mesh spacing (km).",
        },
        "width_of_mfd_bin": {
            "type": float,
            "range": (0.05, 0.5),
            # NATE 2026-06-26: was 0.1, but the deck (services/workers/openquake/
            # job_ini.py render_job_ini signature + render_openquake_deck's
            # build_spec.get fallback) defaults width_of_mfd_bin to 0.2. With
            # advanced_physics=None the registry merges nothing so the deck uses
            # 0.2; an explicit advanced_physics passthrough would inject the
            # registry default. Reconciled to 0.2 (the engine-proven local-run
            # value) so the registry-merged value and the deck default agree.
            "default": 0.2,
            "deck_target": "job.ini:width_of_mfd_bin",
            "doc": "Magnitude-frequency-distribution bin width.",
        },
        "area_source_discretization_km": {
            "type": float,
            "range": (1.0, 50.0),
            "default": 10.0,
            "deck_target": "job.ini:area_source_discretization",
            "doc": "Area-source discretization step (km).",
        },
    },
    # --- Landlab: component-chain numeric knobs (overland / landslide). ---
    "landlab": {
        "overland_alpha": {
            "type": float,
            "range": (0.1, 1.0),
            "default": 0.7,
            "deck_target": "OverlandFlow:alpha",
            "doc": "OverlandFlow stability coefficient (smaller = stabler).",
        },
        "mannings_n": {
            "type": float,
            "range": (0.01, 0.2),
            "default": 0.03,
            "deck_target": "OverlandFlow:mannings_n",
            "doc": "Manning roughness for overland routing.",
        },
        "flow_director": {
            "type": str,
            "range": ("D8", "Dinf", "MFD"),
            "default": "D8",
            "deck_target": "FlowAccumulator:flow_director",
            "doc": "Flow-routing director (D8 / D-infinity / multiple).",
        },
    },
}


def get_engine_physics(engine: str) -> dict[str, dict[str, Any]]:
    """Return the physics table for ``engine`` (raises on an unknown engine)."""
    key = engine.strip().lower()
    table = PHYSICS_REGISTRY.get(key)
    if table is None:
        raise PhysicsRegistryError(
            f"unknown engine {engine!r} (no physics registry); known engines: "
            f"{sorted(PHYSICS_REGISTRY)}",
            engine=engine,
        )
    return table


def _coerce_and_check(engine: str, key: str, spec: dict[str, Any], value: Any) -> Any:
    """Coerce ``value`` to the spec type + range-check it (raises on violation)."""
    want = spec["type"]
    rng = spec.get("range")

    # --- bool: keep strict (a bool is an int subclass, so check it first). --- #
    if want is bool:
        if isinstance(value, bool):
            coerced: Any = value
        elif isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "yes", "on"):
                coerced = True
            elif low in ("false", "0", "no", "off"):
                coerced = False
            else:
                raise PhysicsRegistryError(
                    f"{engine}.{key}: expected a boolean, got {value!r}",
                    engine=engine,
                    key=key,
                )
        else:
            raise PhysicsRegistryError(
                f"{engine}.{key}: expected a boolean, got {value!r}",
                engine=engine,
                key=key,
            )
        return coerced

    if want is str:
        if not isinstance(value, str):
            raise PhysicsRegistryError(
                f"{engine}.{key}: expected a string, got {type(value).__name__}",
                engine=engine,
                key=key,
            )
        coerced = value.strip()
        if rng is not None and coerced not in rng:
            raise PhysicsRegistryError(
                f"{engine}.{key}: {coerced!r} not in allowed values {tuple(rng)}",
                engine=engine,
                key=key,
            )
        return coerced

    # --- numeric (int / float) --- #
    try:
        if want is int:
            # Reject a non-integer float (e.g. 1.5 for an int key) honestly.
            if isinstance(value, bool):
                raise ValueError("bool is not a valid int physics value")
            if isinstance(value, float) and not value.is_integer():
                raise ValueError(f"{value!r} is not an integer")
            coerced = int(value)
        else:
            if isinstance(value, bool):
                raise ValueError("bool is not a valid float physics value")
            coerced = float(value)
    except (TypeError, ValueError) as exc:
        raise PhysicsRegistryError(
            f"{engine}.{key}: cannot coerce {value!r} to {want.__name__}: {exc}",
            engine=engine,
            key=key,
        ) from exc

    if rng is not None:
        lo, hi = rng
        if not (lo <= coerced <= hi):
            raise PhysicsRegistryError(
                f"{engine}.{key}: {coerced} out of range [{lo}, {hi}]",
                engine=engine,
                key=key,
            )
    return coerced


def validate_and_resolve_physics(
    engine: str, overrides: dict[str, Any] | None
) -> dict[str, Any]:
    """Validate + resolve an ``advanced_physics`` overrides dict for ``engine``.

    Returns ``{}`` for ``None`` (the DEFAULT-OFF no-op: no overrides applied,
    deck byte-identical). For a non-empty dict, every key MUST be a registered
    physics key for the engine and every value MUST coerce to the spec type +
    pass the range check; an unknown key or an out-of-range/wrong-type value
    raises :class:`PhysicsRegistryError` (typed, never silently dropped). The
    returned dict carries ONLY the overridden keys (coerced) - it is the
    minimal resolved delta the STEP-3 deck wiring applies on top of the
    engine defaults.
    """
    if overrides is None:
        return {}
    table = get_engine_physics(engine)
    if not isinstance(overrides, dict):
        raise PhysicsRegistryError(
            f"{engine}: advanced_physics must be a dict, got "
            f"{type(overrides).__name__}",
            engine=engine,
        )

    resolved: dict[str, Any] = {}
    for raw_key, value in overrides.items():
        key = str(raw_key).strip()
        spec = table.get(key)
        if spec is None:
            raise PhysicsRegistryError(
                f"{engine}: unknown physics key {key!r}; valid keys: "
                f"{sorted(table)}",
                engine=engine,
                key=key,
            )
        resolved[key] = _coerce_and_check(engine, key, spec, value)
    return resolved


def applied_physics_delta(
    engine: str, resolved: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Return the human-readable DELTA of a resolved overrides dict vs defaults.

    For each overridden key, ``{key: {"from": <engine default>, "to": <resolved>,
    "deck_target": ..., "doc": ...}}`` - the narration the agent surfaces ("you
    changed Kd from 0.0 to 5.0") and the audit trail the deck wiring (STEP 3)
    logs. Keys whose resolved value EQUALS the default are still reported (the
    user explicitly set them); an empty ``resolved`` yields ``{}``.
    """
    if not resolved:
        return {}
    table = get_engine_physics(engine)
    delta: dict[str, dict[str, Any]] = {}
    for key, value in resolved.items():
        spec = table.get(key)
        if spec is None:
            # Should not happen (resolved came from validate); be defensive.
            continue
        delta[key] = {
            "from": spec.get("default"),
            "to": value,
            "deck_target": spec.get("deck_target"),
            "doc": spec.get("doc"),
        }
    return delta
