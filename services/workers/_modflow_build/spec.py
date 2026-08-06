"""The agent -> worker MODFLOW build ``job_spec`` contract (plain JSON dict).

Mirror of ``services/workers/_sfincs_build/spec.py`` (SFINCS reference). The agent
(``run_modflow.compose_and_upload_modflow_build_spec``) composes this from the
confirmed ``MODFLOWRunArgs`` fields (+ the resolved advanced-physics delta),
uploads it to S3, and hands the URI to the ``grace2-modflow`` worker via
``--build-spec-uri``. The worker validates + feeds ``run_args`` into
``build_modflow_deck`` -> mf6 solve -> plume postprocess.

Unlike SFINCS, the build LOGIC is NOT vendored here (it already lives in the
worker's ``gwt_adapter``); this module is purely the schema + a keyword-argument
adapter (``build_deck_kwargs_from_spec``) that turns the JSON ``run_args`` block
into the ``build_modflow_deck`` call kwargs (tuple/list normalization).

Schema (schema_version 1):

    {
      "schema_version": 1,
      "engine": "modflow",
      "spec_id": "<ulid>",
      "run_args": {                                   # -> build_modflow_deck kwargs
        "spill_location_latlon": [lat, lon],          # required
        "contaminant": "benzene",                     # required
        "release_rate_kg_s": 0.5,                     # required
        "duration_days": 30.0,                        # required
        "aquifer_k_ms": 1e-4,                         # optional (adapter default)
        "porosity": 0.3,                              # optional (adapter default)
        "advanced_physics": { ... } | null,           # optional resolved delta
        ...archetype / river-coupling fields...        # optional (ADDITIVE)
      },
      "options": { "compute_class": "standard" }       # sizing/provenance only
    }
"""

from __future__ import annotations

from typing import Any

#: Bumped whenever the job_spec shape changes incompatibly.
JOB_SPEC_SCHEMA_VERSION: int = 1

#: The object key the agent stages the spec under (per run prefix).
JOB_SPEC_FILENAME: str = "modflow_build_spec.json"

#: The run_args fields the worker MUST have to build a spill deck. Everything
#: else in ``run_args`` (aquifer_k_ms / porosity / archetype / river / physics)
#: is optional and passed through verbatim to ``build_modflow_deck``.
_REQUIRED_RUN_ARGS: tuple[str, ...] = (
    "spill_location_latlon",
    "contaminant",
    "release_rate_kg_s",
    "duration_days",
)

#: PARSER VERSION -- bump on a top-level job_spec shape change. Named in the
#: strict-field error (ADR 0158).
_PARSER_VERSION = "modflow-jobspec-1"

#: Every top-level job_spec key this worker reads. Unlike ``run_args`` (an
#: intentionally-open passthrough dict -- see the module docstring: it is
#: unpacked as ``**deck_kwargs`` against ``build_modflow_deck``'s fully-typed
#: signature, so an unknown run_args key ALREADY raises a loud TypeError at the
#: call site; no separate allowlist is needed there), the TOP-LEVEL job_spec
#: keys are extracted via lenient ``.get()`` in this module and would
#: otherwise silently vanish if misnamed (the ADR 0148 failure mode).
_KNOWN_SPEC_FIELDS = frozenset({"schema_version", "engine", "spec_id", "run_args", "options"})


def _reject_unknown_spec_fields(spec: dict[str, Any]) -> None:
    """Raise loudly if ``spec`` carries a top-level key this worker never reads
    (ADR 0158 -- the ADR 0148 lesson applied to the top-level job_spec envelope;
    ``run_args`` internals stay open per the module docstring)."""
    unknown = sorted(set(spec) - _KNOWN_SPEC_FIELDS)
    if unknown:
        raise ValueError(
            f"job_spec carries unknown top-level field(s) {unknown} that parser "
            f"{_PARSER_VERSION} does not read -- this SILENTLY no-ops the "
            f"intended field rather than applying it. Either the caller has a "
            f"typo, or the worker image is stale (rebuild it -- ADR 0148). "
            f"Known top-level fields: {sorted(_KNOWN_SPEC_FIELDS)}."
        )


def validate_job_spec(spec: Any) -> dict[str, Any]:
    """Validate + normalize the agent-composed MODFLOW build job_spec.

    Raises ``ValueError`` on a non-dict body, an unknown ``schema_version``, a
    missing/non-dict ``run_args``, or any missing required run_arg. Returns the
    dict with ``run_args`` / ``options`` defaulted (options -> ``{}`` when
    absent) and ``spill_location_latlon`` coerced to a ``[lat, lon]`` list.
    """
    if not isinstance(spec, dict):
        raise ValueError("job_spec must be a JSON object")
    _reject_unknown_spec_fields(spec)
    sv = spec.get("schema_version")
    if sv is None:
        raise ValueError("job_spec missing schema_version")
    if int(sv) != JOB_SPEC_SCHEMA_VERSION:
        raise ValueError(
            f"unknown job_spec schema_version {sv!r} "
            f"(this worker understands {JOB_SPEC_SCHEMA_VERSION})"
        )

    run_args = spec.get("run_args")
    if not isinstance(run_args, dict):
        raise ValueError("job_spec.run_args must be an object")
    missing = [k for k in _REQUIRED_RUN_ARGS if run_args.get(k) is None]
    if missing:
        raise ValueError(
            f"job_spec.run_args missing required field(s): {sorted(missing)}"
        )

    loc = run_args.get("spill_location_latlon")
    if not isinstance(loc, (list, tuple)) or len(loc) != 2:
        raise ValueError(
            f"job_spec.run_args.spill_location_latlon must be [lat, lon]; got {loc!r}"
        )
    try:
        loc_f = [float(loc[0]), float(loc[1])]
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"job_spec.run_args.spill_location_latlon must be numeric: {exc}"
        ) from exc

    out = dict(spec)
    out_run_args = dict(run_args)
    out_run_args["spill_location_latlon"] = loc_f
    out["run_args"] = out_run_args
    out["options"] = dict(spec.get("options") or {})
    return out


#: The ``build_modflow_deck`` keyword names that carry a lat/lon or polyline the
#: JSON round-trip turns into a list; the adapter accepts list or tuple so no
#: re-tupling is required. Listed here for documentation only.
def build_deck_kwargs_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Turn a validated job_spec into the ``build_modflow_deck`` call kwargs.

    Returns a shallow copy of ``spec['run_args']`` (already normalized by
    ``validate_job_spec``). ``build_modflow_deck`` accepts list-or-tuple for
    every coordinate field, so the JSON-decoded lists pass straight through; the
    ``workdir`` / ``write`` kwargs are supplied by the entrypoint, not the spec.
    """
    run_args = spec.get("run_args")
    if not isinstance(run_args, dict):
        raise ValueError("spec.run_args must be a dict (call validate_job_spec first)")
    kwargs = dict(run_args)
    # Never let the spec smuggle the entrypoint-owned build controls.
    for reserved in ("workdir", "write"):
        kwargs.pop(reserved, None)
    return kwargs
