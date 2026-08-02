"""``set_swmm_parameters`` -- named-parameter SWMM ``.inp`` calibration setter.

Wraps ``swmm_api`` (API verified live against the installed
``swmm-api==0.4.74``): ``read_inp_file`` -> mutate ``SUBCATCHMENTS`` /
``SUBAREAS`` / ``INFILTRATION`` section objects in place -> ``write_file``.
See ``docs/validation/build-contract.md`` section 3.4 for the SetterEnvelope
shape and ``_setter_envelope.py`` for the shared copy-on-write/bounds/publish
machinery this module composes.

ASCII hyphens only.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.tools.simulation._setter_envelope import (
    PhysicalBound,
    SetterInputError,
    SetterUpstreamError,
    build_setter_envelope,
    check_bounds,
    make_work_dir,
    new_child_id,
    publish_child,
    stage_parent,
    utc_now_iso,
)

__all__ = ["set_swmm_parameters", "SWMM_PARAM_BOUNDS"]

logger = logging.getLogger("trid3nt_server.agent.tools.simulation.swmm.set_swmm_parameters.set_swmm_parameters")


#: Physical bounds, named table, per-parameter literature source (lane-D brief).
#: imperviousness: SWMM5 %Imperv is a percentage of subcatchment area by
#: definition (SWMM5 Reference Manual); outside 0-100 is physically meaningless
#: (hard_min=0, hard_max=100) -> hard error.
#: n_imperv/n_perv: overland-flow Manning's n, Chow (1959) compilation; a
#: negative n (hard_min=0) is meaningless -> hard error, out-of-band is a WARNING.
#: infil_rate_*/infil_decay: SWMM5 Horton typical ranges; a negative rate/decay
#: (hard_min=0) is meaningless -> hard error, above-band is a WARNING.
SWMM_PARAM_BOUNDS: dict[str, PhysicalBound] = {
    "imperviousness": PhysicalBound(0.0, 100.0, "%", "SWMM5 %Imperv is a percentage by definition", hard_min=0.0, hard_max=100.0),
    "n_imperv": PhysicalBound(0.01, 0.9, "Manning n", "Chow (1959) overland flow Manning's n compilation", hard_min=0.0),
    "n_perv": PhysicalBound(0.01, 0.9, "Manning n", "Chow (1959) overland flow Manning's n compilation", hard_min=0.0),
    "infil_rate_max": PhysicalBound(0.0, 500.0, "mm/hr", "Horton max infiltration capacity (SWMM5 Reference Manual)", hard_min=0.0),
    "infil_rate_min": PhysicalBound(0.0, 500.0, "mm/hr", "Horton min/final infiltration rate (SWMM5 Reference Manual)", hard_min=0.0),
    "infil_decay": PhysicalBound(0.0, 20.0, "1/hr", "Horton decay constant; SWMM5 manual typical 2-7/hr, extended ceiling", hard_min=0.0),
}

#: parameter -> (section_name, attribute) for SUBCATCHMENTS/SUBAREAS fields.
_SUBCATCHMENT_ATTR: dict[str, tuple[str, str]] = {
    "imperviousness": ("SUBCATCHMENTS", "imperviousness"),
    "n_imperv": ("SUBAREAS", "n_imperv"),
    "n_perv": ("SUBAREAS", "n_perv"),
}
#: parameter -> InfiltrationHorton attribute.
_HORTON_ATTR: dict[str, str] = {
    "infil_rate_max": "rate_max",
    "infil_rate_min": "rate_min",
    "infil_decay": "decay",
}


def _scope_label(targets: list[str], all_ids: list[str]) -> str:
    if set(targets) == set(all_ids):
        return "global"
    if len(targets) <= 3:
        return "zone:" + ",".join(targets)
    return f"zone:{len(targets)}_subcatchments"


def set_swmm_parameters(
    parent_model_uri: str,
    changes: list[dict[str, Any]],
    _work_dir: str | None = None,
    _force_local: bool = False,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Write imperviousness/roughness/Horton-infiltration into a NEW child SWMM ``.inp``.

    **What it does:** Copy-on-write parameter setter for an existing SWMM
    ``.inp`` deck. Copies the parent ``.inp`` into a fresh child file, applies
    named parameter changes to ``SUBCATCHMENTS``/``SUBAREAS``/``INFILTRATION``
    section objects via ``swmm_api``, writes the child, and reports
    before/after values read back from the WRITTEN child file. The parent
    ``.inp`` is never touched -- still runnable afterward.

    **When to use:** calibrating/adjusting a staged SWMM model before
    ``run_solver`` -- "raise percent impervious to 60% on these
    subcatchments", "halve the Horton max infiltration rate site-wide",
    "set pervious Manning's n to 0.15".

    **When NOT to use:** building a new SWMM deck from scratch (a separate
    workflow concern); a deck whose ``[INFILTRATION]`` method is not Horton
    (v1 supports Horton only -- raises a typed error naming the actual
    method rather than silently reinterpreting Green-Ampt/CurveNumber
    fields); dispatching a run (use ``run_solver`` on the returned
    ``model_root_uri``).

    **Parameters:**
    - ``parent_model_uri`` (str): the parent ``.inp`` file -- ``s3://...``,
      ``file:///...``, or a bare local path. Required.
    - ``changes`` (list[dict]): one or more
      ``{"parameter": "imperviousness"|"n_imperv"|"n_perv"|"infil_rate_max"|
      "infil_rate_min"|"infil_decay", "op": "set"|"scale", "value": <float,
      required for set>, "factor": <float, required for scale>,
      "subcatchments": [<id>, ...] (optional; omitted = ALL subcatchments,
      i.e. global scope)}``. Horton infiltration params require the target
      subcatchment(s) to already carry an ``[INFILTRATION]`` entry (v1
      modifies existing entries only; does not fabricate new Horton triples).

    **Returns:** SetterEnvelope dict ``{engine="swmm", child_setup_uri,
    parent_model, changes_applied[], plausibility[], notes[]}`` (see
    build-contract.md section 3.4). ``scope`` is ``"global"`` or
    ``"zone:<ids>"``/``"zone:<n>_subcatchments"``. Multi-subcatchment
    before/after are the MEAN across the targeted set (each subcatchment is
    individually scaled off its own prior value, not the mean).

    **Raises:** ``SetterInputError`` (unknown parameter, bad op, missing
    value/factor, unknown subcatchment id, missing INFILTRATION entry, or a
    non-Horton infiltration method); ``BoundsViolation`` (a physically
    MEANINGLESS value only -- percent-impervious outside 0-100, a negative
    Manning's n / infiltration rate; an atypical-but-physical value outside a
    plausible band is carried as a WARNING with ``plausibility[].in_range=false``
    and proceeds); ``SetterUpstreamError`` (swmm_api not importable, deck
    read/write failed).

    **Cross-tool dependencies:** consumes a staged SWMM ``.inp``; the
    child's ``model_root_uri`` points at the written child ``.inp``, ready
    for a follow-up dispatch-manifest build (out of scope for this atomic
    setter).
    """
    if not isinstance(changes, list) or not changes:
        raise SetterInputError("changes must be a non-empty list of change dicts")

    try:
        from swmm_api import read_inp_file
        from swmm_api.input_file.sections import InfiltrationHorton
    except Exception as exc:  # noqa: BLE001
        raise SetterUpstreamError(f"swmm_api not importable: {exc}") from exc

    work_dir = make_work_dir(_work_dir)
    child_id = new_child_id()
    child_root = work_dir / child_id
    child_model_dir = child_root / "model"
    child_inp_path = stage_parent(parent_model_uri, child_model_dir, is_dir=False)

    try:
        inp = read_inp_file(str(child_inp_path))
    except Exception as exc:  # noqa: BLE001
        raise SetterUpstreamError(f"SWMM .inp read failed at {child_inp_path}: {exc}") from exc

    # NOTE: SwmmInput.get(...) is NOT dict-like (it returns raw section text);
    # membership + bracket access ARE dict-like (a present-but-empty section
    # -- or an absent one with a known name -- both resolve to {} via
    # __getitem__). Verified live against swmm-api==0.4.74.
    if "SUBCATCHMENTS" not in inp or not inp["SUBCATCHMENTS"]:
        raise SetterUpstreamError(f"{child_inp_path} has no [SUBCATCHMENTS] section")
    subcatch = inp["SUBCATCHMENTS"]
    all_ids = list(subcatch.keys())
    subareas = inp["SUBAREAS"] if "SUBAREAS" in inp else {}
    infiltration = inp["INFILTRATION"] if "INFILTRATION" in inp else {}

    baseline: dict[str, float] = {}  # parameter -> mean value at first touch
    touched_targets: dict[str, list[str]] = {}  # parameter -> union of target ids touched
    plausibility_by_param: dict[str, dict[str, Any]] = {}
    notes: list[str] = []

    for change in changes:
        if not isinstance(change, dict):
            raise SetterInputError(f"each change must be a dict; got {change!r}")
        param = change.get("parameter")
        op = change.get("op")
        if param not in SWMM_PARAM_BOUNDS:
            raise SetterInputError(
                f"unknown swmm parameter {param!r}; supported: {sorted(SWMM_PARAM_BOUNDS)}"
            )
        if op not in ("set", "scale"):
            raise SetterInputError(f"op must be 'set' or 'scale'; got {op!r}")
        targets = change.get("subcatchments") or list(all_ids)
        if not isinstance(targets, list) or not targets:
            raise SetterInputError("subcatchments, when given, must be a non-empty list")
        for tid in targets:
            if tid not in subcatch:
                raise SetterInputError(f"unknown subcatchment id {tid!r} (parent has: {sorted(all_ids)})")

        if op == "set" and "value" not in change:
            raise SetterInputError(f"op='set' requires 'value' for parameter {param!r}")
        if op == "scale" and "factor" not in change:
            raise SetterInputError(f"op='scale' requires 'factor' for parameter {param!r}")

        new_values: dict[str, float] = {}
        old_values: dict[str, float] = {}
        for tid in targets:
            if param in _SUBCATCHMENT_ATTR:
                section_name, attr = _SUBCATCHMENT_ATTR[param]
                obj = subcatch[tid] if section_name == "SUBCATCHMENTS" else subareas.get(tid) if subareas else None
                if obj is None:
                    raise SetterInputError(f"subcatchment {tid!r} has no [SUBAREAS] entry")
                old = float(getattr(obj, attr))
            else:  # Horton infiltration
                if not infiltration or tid not in infiltration:
                    raise SetterInputError(
                        f"subcatchment {tid!r} has no [INFILTRATION] entry -- v1 modifies "
                        "existing Horton entries only, it does not fabricate new ones"
                    )
                entry = infiltration[tid]
                if not isinstance(entry, InfiltrationHorton):
                    raise SetterInputError(
                        f"subcatchment {tid!r} infiltration method is "
                        f"{type(entry).__name__}, not Horton -- v1 supports Horton only"
                    )
                old = float(getattr(entry, _HORTON_ATTR[param]))
            old_values[tid] = old
            new_values[tid] = float(change["value"]) if op == "set" else old * float(change["factor"])

        for tid, v in new_values.items():
            check_bounds(engine="swmm", param=param, value=v, table=SWMM_PARAM_BOUNDS)

        # apply-as-you-go: immediately write into the section objects so a
        # SUBSEQUENT change in this same call sees the updated "current" value.
        for tid, v in new_values.items():
            if param in _SUBCATCHMENT_ATTR:
                section_name, attr = _SUBCATCHMENT_ATTR[param]
                obj = subcatch[tid] if section_name == "SUBCATCHMENTS" else subareas[tid]
                setattr(obj, attr, v)
            else:
                setattr(infiltration[tid], _HORTON_ATTR[param], v)

        if param not in baseline:
            baseline[param] = sum(old_values.values()) / len(old_values)
            touched_targets[param] = []
        for tid in targets:
            if tid not in touched_targets[param]:
                touched_targets[param].append(tid)
        mean_new = sum(new_values.values()) / len(new_values)
        plausibility_by_param[param] = check_bounds(
            engine="swmm", param=param, value=mean_new, table=SWMM_PARAM_BOUNDS
        )

    try:
        inp.write_file(str(child_inp_path))
    except Exception as exc:  # noqa: BLE001
        raise SetterUpstreamError(f"SWMM .inp write failed at {child_inp_path}: {exc}") from exc

    try:
        reread = read_inp_file(str(child_inp_path))
    except Exception as exc:  # noqa: BLE001
        raise SetterUpstreamError(f"SWMM .inp re-read (write verification) failed: {exc}") from exc
    re_subcatch = reread["SUBCATCHMENTS"]
    re_subareas = reread["SUBAREAS"] if "SUBAREAS" in reread else {}
    re_infiltration = reread["INFILTRATION"] if "INFILTRATION" in reread else {}

    changes_applied: list[dict[str, Any]] = []
    for param, targets in touched_targets.items():
        after_values = []
        for tid in targets:
            if param in _SUBCATCHMENT_ATTR:
                section_name, attr = _SUBCATCHMENT_ATTR[param]
                obj = re_subcatch[tid] if section_name == "SUBCATCHMENTS" else re_subareas[tid]
                after_values.append(float(getattr(obj, attr)))
            else:
                after_values.append(float(getattr(re_infiltration[tid], _HORTON_ATTR[param])))
        unit = SWMM_PARAM_BOUNDS[param].unit
        changes_applied.append({
            "param": param,
            "scope": _scope_label(targets, all_ids),
            "before": round(baseline[param], 6),
            "after": round(sum(after_values) / len(after_values), 6),
            "unit": unit,
        })

    manifest = {
        "schema_version": "v1",
        "engine": "swmm",
        "child_id": child_id,
        "parent_model": parent_model_uri,
        "changes_applied": changes_applied,
        "created_at": utc_now_iso(),
    }
    publish = publish_child(
        child_root,
        engine="swmm",
        child_id=child_id,
        manifest=manifest,
        prefer_s3=parent_model_uri.startswith("s3://") and not _force_local,
    )
    notes.append(f"child .inp staged {publish['storage']}; parent left immutable (A.7 replace-not-reconcile)")
    notes.append(f"model_root_uri={publish['model_root_uri']} model_file={child_inp_path.name}")

    return build_setter_envelope(
        engine="swmm",
        child_setup_uri=publish["child_setup_uri"],
        parent_model=parent_model_uri,
        changes_applied=changes_applied,
        plausibility=list(plausibility_by_param.values()),
        notes=notes,
    )


set_swmm_parameters = register_tool(
    AtomicToolMetadata(
        name="set_swmm_parameters",
        ttl_class="live-no-cache",
        source_class="param_setter",
        cacheable=False,
    ),
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)(set_swmm_parameters)
