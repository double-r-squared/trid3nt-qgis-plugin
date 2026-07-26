"""``set_telemac_parameters`` -- named-parameter TELEMAC-2D bed-friction setter.

Copy-on-write calibration setter for a TELEMAC-2D steering file (``.cas``).
Unlike SFINCS/SWMM/MODFLOW there is no engine Python package that round-trips
a ``.cas``: TELEMAC's steering deck is a flat ``KEYWORD = value`` text file
(``/`` line comments, ``&FIN`` terminator, ``=`` OR ``:`` separators). So this
module hand-parses the two bed-friction keywords, rewrites ONLY their value
tokens in place (every other line copied byte-for-byte), and reads the written
values back. The ``.cas`` references its mesh (``GEOMETRY FILE``) and boundary
(``BOUNDARY CONDITIONS FILE``) siblings by relative name, so staging copies the
whole containing directory (the "child case dir") -- the child deck stays
runnable. See ``docs/validation/build-contract.md`` section 3.4 for the
SetterEnvelope shape and ``_setter_envelope.py`` for the shared copy-on-write /
bounds / publish machinery this module composes (ADR 0022).

v1 KNOBS: ``friction_law`` (LAW OF BOTTOM FRICTION -- 2=Chezy, 3=Strickler,
4=Manning) and the global ``friction_coefficient`` (FRICTION COEFFICIENT).
Zone-based (spatially varying) friction is OUT of v1 scope -- TELEMAC does that
via a FRICTION DATA FILE + a user-fortran ``strche`` law-per-zone, which is a
separate deck-authoring concern, not a value edit.

THE CLASSIC ERROR (documented, guarded): the friction COEFFICIENT's meaning is
LAW-DEPENDENT. Under Strickler (law 3) it is the Strickler coefficient
``Ks`` [m^(1/3)/s] and HIGHER = SMOOTHER bed. Under Manning (law 4) it is the
Manning ``n`` [s/m^(1/3)] and is the INVERSE (``n = 1/Ks``), so higher = ROUGHER
and the plausible band is ~0.011-0.1, NOT 15-90. Under Chezy (law 2) it is the
Chezy ``C`` [m^(1/2)/s], higher = smoother. So the plausible band is chosen
from the EFFECTIVE law (the law being set this call, else the deck's current
law); changing the law to Manning while leaving ``Ks=30`` is flagged
in_range=false (a Manning n of 30 is absurd) rather than silently accepted.

ASCII hyphens only.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from .. import register_tool
from ._setter_envelope import (
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

__all__ = ["set_telemac_parameters", "TELEMAC_FRICTION_LAWS", "friction_coefficient_bound"]

logger = logging.getLogger("trid3nt_server.tools.simulation.set_telemac_parameters")


# --------------------------------------------------------------------------- #
# Bounds. friction_coefficient's plausible band is LAW-DEPENDENT (see module
# docstring). physics_registry.py's telemac.friction_coefficient advanced-
# physics lever pins the Strickler-only band (10,90); this setter GENERALIZES
# it to be law-aware and adds the hard floor (coefficient <= 0 is meaningless).
# --------------------------------------------------------------------------- #

#: v1 supported friction laws -> the coefficient's physical meaning + unit.
#: TELEMAC LAW OF BOTTOM FRICTION also allows 0 (none)/1 (Haaland)/5 (Nikuradse)
#: /6 (log)/7 (Colebrook-White), but those are out of v1 scope (0/1/5+ do not
#: take a single Chezy/Strickler/Manning coefficient); an out-of-set law is a
#: typed SetterInputError, not a silent reinterpretation.
TELEMAC_FRICTION_LAWS: dict[int, str] = {2: "Chezy", 3: "Strickler", 4: "Manning"}

# Strickler K plausible band 15-90 m^(1/3)/s: natural channels ~20-40, smooth
# artificial ~70-90 (Chow 1959 Manning-n compilation via Ks = 1/n; TELEMAC-2D
# user manual friction section). Chezy C uses the same numeric envelope (both
# rise with smoothness). Manning n plausible band 0.011-0.1 = 1/Ks of the same
# range extended to a rough floodplain (Chow 1959). A coefficient <= 0 is
# physically meaningless under EVERY law (hard floor, exclusive).
_STRICKLER_BOUND = PhysicalBound(
    15.0, 90.0, "m^(1/3)/s (Strickler Ks)",
    "Strickler Ks plausible band 15-90 (natural ~20-40, smooth artificial ~70-90; "
    "Chow 1959 via Ks=1/n / TELEMAC-2D user manual). Higher Ks = SMOOTHER bed.",
    hard_min=0.0, hard_min_exclusive=True,
)
_CHEZY_BOUND = PhysicalBound(
    15.0, 90.0, "m^(1/2)/s (Chezy C)",
    "Chezy C plausible band 15-90 for natural-to-smooth channels (Chow 1959). "
    "Higher C = SMOOTHER bed.",
    hard_min=0.0, hard_min_exclusive=True,
)
_MANNING_BOUND = PhysicalBound(
    0.011, 0.1, "s/m^(1/3) (Manning n)",
    "Manning n plausible band 0.011-0.1 (Chow 1959 overland/channel; n=1/Ks, the "
    "INVERSE of Strickler). Higher n = ROUGHER bed.",
    hard_min=0.0, hard_min_exclusive=True,
)
_LAW_BOUND: dict[int, PhysicalBound] = {
    2: _CHEZY_BOUND,
    3: _STRICKLER_BOUND,
    4: _MANNING_BOUND,
}


def friction_coefficient_bound(law: int) -> PhysicalBound:
    """Return the plausible band + hard floor for FRICTION COEFFICIENT under
    ``law`` (2=Chezy, 3=Strickler, 4=Manning). Raises ``SetterInputError`` for
    an out-of-v1-scope law id (the coefficient's meaning is undefined there)."""
    bound = _LAW_BOUND.get(int(law))
    if bound is None:
        raise SetterInputError(
            f"friction_law {law!r} has no v1 coefficient band; supported laws: "
            f"{TELEMAC_FRICTION_LAWS}"
        )
    return bound


# --------------------------------------------------------------------------- #
# .cas steering-file parse/edit (pure text; no TELEMAC/docker needed).
# --------------------------------------------------------------------------- #

_LAW_KEYWORD = "LAW OF BOTTOM FRICTION"
_COEF_KEYWORD = "FRICTION COEFFICIENT"
_TERMINATORS = ("&FIN", "&ETA", "&LIS", "&STO")


def _normalize_kw(text: str) -> str:
    """Uppercase + collapse internal whitespace (TELEMAC keyword-compare form)."""
    return " ".join(text.upper().split())


def _sep_index(line: str) -> int | None:
    """Index of the FIRST ``=`` or ``:`` value separator, or None."""
    candidates = [i for i in (line.find("="), line.find(":")) if i != -1]
    return min(candidates) if candidates else None


def _parse_kw_line(line: str) -> tuple[str, str] | None:
    """``(normalized_keyword, value_str)`` for a ``KEYWORD = value`` line, or
    None for a blank / ``/`` comment / ``&`` directive / no-separator line."""
    stripped = line.strip()
    if not stripped or stripped.startswith("/") or stripped.startswith("&"):
        return None
    sep = _sep_index(line)
    if sep is None:
        return None
    return _normalize_kw(line[:sep]), line[sep + 1:].strip()


def _first_token_int(value_str: str) -> int:
    return int(float(value_str.split()[0]))


def _first_token_float(value_str: str) -> float:
    return float(value_str.split()[0])


def _read_friction(lines: list[str]) -> tuple[int | None, float | None]:
    """Current (law, coefficient) as written in the deck, or None per field
    when the keyword is absent."""
    law: int | None = None
    coeff: float | None = None
    for line in lines:
        parsed = _parse_kw_line(line)
        if parsed is None:
            continue
        kw, val = parsed
        try:
            if kw == _LAW_KEYWORD and val.split():
                law = _first_token_int(val)
            elif kw == _COEF_KEYWORD and val.split():
                coeff = _first_token_float(val)
        except (ValueError, IndexError) as exc:
            raise SetterUpstreamError(
                f"unparseable {kw!r} value in .cas line {line.strip()!r}: {exc}"
            ) from exc
    return law, coeff


def _fmt_coeff(value: float) -> str:
    """Compact TELEMAC float literal (keeps a decimal point: 40.0 -> '40.')."""
    s = f"{float(value):g}"
    if not any(c in s for c in ".eE"):
        s += "."
    return s


def _rewrite_value(line: str, new_value_str: str) -> str:
    """Replace only the value token of a keyword line, preserving the keyword
    text, the original separator char, and the whitespace before the value."""
    sep = _sep_index(line)
    assert sep is not None  # only called on a matched keyword line
    head = line[: sep + 1]
    rest = line[sep + 1:]
    lead_ws = rest[: len(rest) - len(rest.lstrip())]
    return head + lead_ws + new_value_str


def _edit_cas(
    lines: list[str],
    *,
    new_law: int | None,
    new_coeff_str: str | None,
) -> tuple[list[str], list[str]]:
    """Return (edited_lines, notes). In-place value rewrite for a present
    keyword; a requested change to an ABSENT keyword is inserted before the
    ``&FIN`` terminator (recorded in notes)."""
    out: list[str] = []
    notes: list[str] = []
    law_seen = False
    coeff_seen = False
    for line in lines:
        parsed = _parse_kw_line(line)
        if parsed is not None:
            kw = parsed[0]
            if kw == _LAW_KEYWORD and new_law is not None:
                out.append(_rewrite_value(line, str(int(new_law))))
                law_seen = True
                continue
            if kw == _COEF_KEYWORD and new_coeff_str is not None:
                out.append(_rewrite_value(line, new_coeff_str))
                coeff_seen = True
                continue
            if kw == _LAW_KEYWORD:
                law_seen = True
            elif kw == _COEF_KEYWORD:
                coeff_seen = True
        out.append(line)

    inserts: list[str] = []
    if new_law is not None and not law_seen:
        inserts.append(f"{_LAW_KEYWORD} = {int(new_law)}")
        notes.append(
            f"deck had no {_LAW_KEYWORD} line; inserted it (before=null)"
        )
    if new_coeff_str is not None and not coeff_seen:
        inserts.append(f"{_COEF_KEYWORD} = {new_coeff_str}")
        notes.append(
            f"deck had no {_COEF_KEYWORD} line; inserted it (before=null)"
        )
    if inserts:
        out = _insert_before_terminator(out, inserts)
    return out, notes


def _insert_before_terminator(lines: list[str], inserts: list[str]) -> list[str]:
    for i, line in enumerate(lines):
        if line.strip().upper().startswith(_TERMINATORS):
            return lines[:i] + inserts + lines[i:]
    return lines + inserts


# --------------------------------------------------------------------------- #
# Case-dir staging (parent .cas + its mesh/boundary siblings).
# --------------------------------------------------------------------------- #


def _split_cas_uri(uri: str) -> tuple[str, str]:
    """Split a ``.cas`` handle into (containing_dir_uri, cas_basename).

    ``s3://bucket/pre/t2d.cas`` -> (``s3://bucket/pre/``, ``t2d.cas``);
    ``file:///d/t2d.cas`` / a bare path -> (``/d``, ``t2d.cas``). Raises
    ``SetterInputError`` unless the handle names a ``.cas`` file."""
    if not isinstance(uri, str) or not uri.strip():
        raise SetterInputError(f"parent_model_uri must be a non-empty string; got {uri!r}")
    if uri.startswith("s3://"):
        rest = uri[len("s3://"):]
        bucket, _, key = rest.partition("/")
        if not bucket or not key.lower().endswith(".cas"):
            raise SetterInputError(
                f"s3:// parent_model_uri must name a .cas steering file; got {uri!r}"
            )
        parent_key, _, base = key.rpartition("/")
        dir_uri = f"s3://{bucket}/{parent_key}/" if parent_key else f"s3://{bucket}/"
        return dir_uri, base
    raw = uri[len("file://"):] if uri.startswith("file://") else uri
    p = Path(raw)
    if not p.name.lower().endswith(".cas"):
        raise SetterInputError(
            f"parent_model_uri must name a .cas steering file; got {uri!r}"
        )
    return str(p.parent), p.name


def set_telemac_parameters(
    parent_model_uri: str,
    changes: list[dict[str, Any]],
    _work_dir: str | None = None,
    _force_local: bool = False,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Write bed-friction law/coefficient into a NEW child TELEMAC-2D deck.

    **What it does:** Copy-on-write parameter setter for an existing TELEMAC-2D
    steering file (``.cas``). Copies the parent case directory (the ``.cas`` +
    its ``GEOMETRY FILE`` mesh and ``BOUNDARY CONDITIONS FILE`` siblings) into a
    fresh child case dir, rewrites the friction keyword values in the child
    ``.cas`` (every other line byte-identical), and reports before/after values
    READ BACK from the WRITTEN child deck. The parent deck is never touched --
    still runnable afterward.

    **When to use:** calibrating bed roughness before a re-run of a TELEMAC-2D
    case (e.g. the Malpasset dam-break V&V case) -- "increase the river bed
    roughness before rerunning", "set the Strickler friction coefficient to 40",
    "switch the friction law to Manning with n=0.033".

    **When NOT to use:** building a NEW deck / mesh from scratch; spatially
    varying (zone-based) friction (v1 is a single GLOBAL coefficient -- zone
    friction needs a FRICTION DATA FILE + user-fortran, out of scope); any
    non-friction keyword (time step, turbulence, advection -- not exposed here);
    dispatching a run (drive the returned child deck through the solver
    separately).

    **Parameters:**
    - ``parent_model_uri`` (str): the parent ``.cas`` steering file --
      ``s3://.../t2d.cas``, ``file:///.../t2d.cas``, or a bare local path.
      Required. Its whole containing directory is staged (copy-on-write).
    - ``changes`` (list[dict]): one or more
      ``{"parameter": "friction_coefficient"|"friction_law", "op":
      "set"|"scale", "value": <float/int, required for set>, "factor": <float,
      required for scale>}``. ``friction_law`` is an enumerated id
      (2=Chezy, 3=Strickler, 4=Manning) and accepts op="set" only. The
      coefficient's plausible band is chosen from the EFFECTIVE law (the law
      set this call, else the deck's current law), so changing law without
      re-checking the coefficient cannot silently mis-scale roughness.
      Duplicate entries for a parameter collapse to the last one.

    **Returns:** SetterEnvelope dict ``{engine="telemac", child_setup_uri,
    parent_model, changes_applied[], plausibility[], notes[]}`` (see
    build-contract.md section 3.4). ``changes_applied[].before``/``.after`` are
    read back from the written child ``.cas`` (before=null when the keyword was
    absent in the parent and inserted). ``notes[]`` carries the child case dir's
    ``model_root_uri`` and ``.cas`` filename.

    **Raises:** ``SetterInputError`` (unknown parameter, bad op, missing
    value/factor, an out-of-v1-scope friction law, scale on friction_law, a
    parent handle that is not a ``.cas`` file); ``BoundsViolation`` (a
    physically MEANINGLESS coefficient only -- ``<= 0`` under any law; an
    atypical-but-physical value outside the law's plausible band, Strickler/
    Chezy 15-90 or Manning 0.011-0.1, is carried as a WARNING with
    ``plausibility[].in_range=false`` and proceeds); ``SetterUpstreamError``
    (parent staging or child read/write failed, unparseable friction line).

    **Cross-tool dependencies:** consumes a staged TELEMAC-2D case dir; the
    child's ``model_root_uri`` is a case dir shaped the same way (``.cas`` +
    mesh + boundary), ready to dispatch to the TELEMAC solver (out of scope for
    this atomic setter).
    """
    if not isinstance(changes, list) or not changes:
        raise SetterInputError("changes must be a non-empty list of change dicts")

    dir_uri, cas_basename = _split_cas_uri(parent_model_uri)

    work_dir = make_work_dir(_work_dir)
    child_id = new_child_id()
    child_root = work_dir / child_id
    child_model_dir = child_root / "model"
    stage_parent(dir_uri, child_model_dir, is_dir=True)
    child_cas = child_model_dir / cas_basename
    if not child_cas.is_file():
        raise SetterUpstreamError(
            f"staged case dir has no {cas_basename!r} (parent_model_uri did not "
            "resolve to a .cas inside a case directory)"
        )

    try:
        lines = child_cas.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SetterUpstreamError(f"child .cas read failed at {child_cas}: {exc}") from exc

    law_before, coeff_before = _read_friction(lines)

    # --- resolve requested changes (last-wins per param) --------------------- #
    requested_law: int | None = None
    coeff_op: str | None = None
    coeff_operand: float | None = None
    touched: list[str] = []
    for change in changes:
        if not isinstance(change, dict):
            raise SetterInputError(f"each change must be a dict; got {change!r}")
        param = change.get("parameter")
        op = change.get("op")
        if param not in ("friction_coefficient", "friction_law"):
            raise SetterInputError(
                f"unknown telemac parameter {param!r}; supported: "
                "['friction_coefficient', 'friction_law']"
            )
        if op not in ("set", "scale"):
            raise SetterInputError(f"op must be 'set' or 'scale'; got {op!r}")
        touched.append(param)
        if param == "friction_law":
            if op != "set":
                raise SetterInputError(
                    "friction_law is an enumerated law id; use op='set' (scale is "
                    "meaningless for an enum)"
                )
            if "value" not in change:
                raise SetterInputError("op='set' requires 'value' for friction_law")
            law_val = int(change["value"])
            if law_val not in TELEMAC_FRICTION_LAWS:
                raise SetterInputError(
                    f"friction_law must be one of {TELEMAC_FRICTION_LAWS} in v1; "
                    f"got {law_val}"
                )
            requested_law = law_val
        else:  # friction_coefficient
            if op == "set":
                if "value" not in change:
                    raise SetterInputError(
                        "op='set' requires 'value' for friction_coefficient"
                    )
                coeff_op, coeff_operand = "set", float(change["value"])
            else:
                if "factor" not in change:
                    raise SetterInputError(
                        "op='scale' requires 'factor' for friction_coefficient"
                    )
                coeff_op, coeff_operand = "scale", float(change["factor"])

    notes: list[str] = []
    if len(touched) != len(set(touched)):
        notes.append("duplicate parameter entries in changes[] collapsed to the last value")

    # --- effective law drives the coefficient's plausible band --------------- #
    if requested_law is not None:
        effective_law = requested_law
    elif law_before is not None:
        effective_law = law_before
    else:
        effective_law = 3  # TELEMAC's common default; deck carried no law line
        notes.append(
            "deck carried no LAW OF BOTTOM FRICTION line; assuming Strickler (3) "
            "for coefficient plausibility"
        )

    plausibility: list[dict[str, Any]] = []
    new_coeff: float | None = None
    new_coeff_str: str | None = None

    if coeff_op is not None:
        if coeff_op == "set":
            new_coeff = float(coeff_operand)  # type: ignore[arg-type]
        else:
            if coeff_before is None:
                raise SetterInputError(
                    "op='scale' on friction_coefficient requires a FRICTION "
                    "COEFFICIENT already present in the parent deck (nothing to scale)"
                )
            new_coeff = coeff_before * float(coeff_operand)  # type: ignore[arg-type]
        bound = friction_coefficient_bound(effective_law)
        # HARD BoundsViolation on <= 0; else a section-3.4 plausibility entry.
        entry = check_bounds(
            engine="telemac",
            param="friction_coefficient",
            value=new_coeff,
            table={"friction_coefficient": bound},
        )
        plausibility.append(entry)
        new_coeff_str = _fmt_coeff(new_coeff)

    # Law changed but coefficient NOT re-set: re-check the EXISTING coefficient
    # against the NEW law (the classic misinterpretation trap -- e.g. leaving
    # Ks=30 while switching to Manning, where 30 is an absurd n).
    if requested_law is not None and coeff_op is None and coeff_before is not None:
        bound = friction_coefficient_bound(effective_law)
        entry = check_bounds(
            engine="telemac",
            param="friction_coefficient",
            value=coeff_before,
            table={"friction_coefficient": bound},
        )
        plausibility.append(entry)
        if not entry["in_range"]:
            notes.append(
                f"friction_law changed to {TELEMAC_FRICTION_LAWS[effective_law]} "
                f"({effective_law}) but FRICTION COEFFICIENT left at {coeff_before:g}, "
                f"which is outside the {TELEMAC_FRICTION_LAWS[effective_law]} plausible "
                "band -- the coefficient's meaning changed with the law"
            )

    # --- write the child .cas ----------------------------------------------- #
    edited, edit_notes = _edit_cas(lines, new_law=requested_law, new_coeff_str=new_coeff_str)
    notes.extend(edit_notes)
    try:
        child_cas.write_text("\n".join(edited) + "\n", encoding="utf-8")
    except OSError as exc:
        raise SetterUpstreamError(f"child .cas write failed at {child_cas}: {exc}") from exc

    # --- read back from the WRITTEN child (never echo the request) ----------- #
    reread = child_cas.read_text(encoding="utf-8").splitlines()
    law_after, coeff_after = _read_friction(reread)

    changes_applied: list[dict[str, Any]] = []
    if requested_law is not None:
        changes_applied.append({
            "param": "friction_law",
            "scope": "global",
            "before": law_before,
            "after": law_after,
            "unit": "law-id (2=Chezy,3=Strickler,4=Manning)",
        })
    if coeff_op is not None:
        unit = friction_coefficient_bound(effective_law).unit
        changes_applied.append({
            "param": "friction_coefficient",
            "scope": "global",
            "before": round(coeff_before, 6) if coeff_before is not None else None,
            "after": round(coeff_after, 6) if coeff_after is not None else None,
            "unit": unit,
        })

    notes.append(
        "zone-based (spatially varying) friction is out of v1 scope; "
        "friction_coefficient sets the single GLOBAL FRICTION COEFFICIENT"
    )

    manifest = {
        "schema_version": "v1",
        "engine": "telemac",
        "child_id": child_id,
        "parent_model": parent_model_uri,
        "cas_file": cas_basename,
        "changes_applied": changes_applied,
        "created_at": utc_now_iso(),
    }
    publish = publish_child(
        child_root,
        engine="telemac",
        child_id=child_id,
        manifest=manifest,
        prefer_s3=parent_model_uri.startswith("s3://") and not _force_local,
    )
    notes.append(f"child case dir staged {publish['storage']}; parent left immutable (A.7 replace-not-reconcile)")
    notes.append(f"model_root_uri={publish['model_root_uri']} cas_file={cas_basename}")

    return build_setter_envelope(
        engine="telemac",
        child_setup_uri=publish["child_setup_uri"],
        parent_model=parent_model_uri,
        changes_applied=changes_applied,
        plausibility=plausibility,
        notes=notes,
    )


set_telemac_parameters = register_tool(
    AtomicToolMetadata(
        name="set_telemac_parameters",
        ttl_class="live-no-cache",
        source_class="param_setter",
        cacheable=False,
    ),
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)(set_telemac_parameters)
