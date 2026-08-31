"""The AUTHOR step: the accepted mesh + the approved sheet -> TELEMAC's own decks.

A ``.cas`` is a RECORD of the run - which boundary carries the flowrate, what the
friction law is, which module is coupled - and a record has one author. That
author is here, on the server, beside the sheet the numbers came from; the worker
receives files and runs them.

Two facts make single-pass authoring possible, and both are measured before this
module is called:

  * the LIQUID-BOUNDARY ORDER. TELEMAC numbers its liquid boundaries by walking
    the contours, and the walk does not start at the inflow. The pair writer
    reports the order it measured off the ``.cli`` it just wrote, so the
    PRESCRIBED lists are written in that order rather than probed for by a
    throwaway solve.
  * the RELEASE POINT, settled against the domain polygon and the flowline while
    the user could still move it.

Every optional block is emitted ONLY when it was asked for, so a run that uses no
module writes the deck it always wrote. Every line respects DAMOCLES's hard
72-character limit: one long line derails the parser onto a later, valid line and
the error names the wrong keyword.
"""

from __future__ import annotations

import datetime
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.author")

__all__ = [
    "GAIA_RESULT_FILENAME",
    "GAIA_STEERING_FILENAME",
    "NESTOR_TIME_ORIGIN",
    "SOURCES_FILENAME",
    "WAQTEL_FILENAME",
    "author_reach_deck",
    "author_rog_deck",
    "write_cn_map",
    "write_friction_files",
    "write_gaia_deck",
    "write_hyetograph_file",
    "write_nestor_decks",
    "write_oil_inputs",
    "write_sources_pulse",
    "write_waqtel_decay",
    "write_waqtel_o2",
]

SOURCES_FILENAME = "river_sources.txt"
#: The WAQTEL steering file - its own DAMOCLES-parsed deck, named in the t2d cas.
WAQTEL_FILENAME = "t2d_river.waqtel"
#: The GAIA steering file and the result SELAFIN carrying CUMUL BED EVOL.
GAIA_STEERING_FILENAME = "gaia_river.cas"
GAIA_RESULT_FILENAME = "gaia_river.slf"
NESTOR_ACTION_FILENAME = "nestor.act"
NESTOR_POLYGON_FILENAME = "nestor.pol"
NESTOR_SURFACE_REF_FILENAME = "nestor.ref"
#: The time origin stamped into the t2d deck so NESTOR's absolute action dates
#: map to sim seconds through DateStringToSeconds (seconds since MARDAT/MARTIM).
NESTOR_TIME_ORIGIN = (2024, 1, 1, 0, 0, 0)
#: NESTOR matches a polygon NAME to an action's FieldDig/FieldDump on the first
#: three numerals, and its ThreeDigitsNumeral check rejects a leading zero.
_NESTOR_DIG_FIELD = "101_channel"
_NESTOR_DUMP_FIELD = "102_spoil"

#: DAMOCLES reads no more than this many characters of a line.
_CAS_LINE_LIMIT = 72
#: How a comment opens in the non-DAMOCLES files below - a polygon fence, a
#: profile fence, a scatter, a hyetograph. Every one of those readers keys on the
#: leading character alone, and it carries no space after it: a hash followed by
#: a space is markdown, and nothing here is ever read as markdown.
_FILE_COMMENT = "#"

#: What the deck leaves unsaid. These are the values the run is solved at when
#: the sheet states none - the physics defaults, in the one place a reader can
#: find them, rather than in a config object inside the container.
_DEFAULTS: dict[str, Any] = {
    "name": "reach",
    "init_depth_m": 2.0,
    "inflow_q_m3s": 50.0,
    "substance_class": "tracer",
    "erodible_bed": False,
    "dredging": False,
    "spill_frac": 0.25,
    "pulse_window_s": 300.0,
    "source_q_m3s": 8.0,
    "dye_conc_mgl": 100.0,
    "duration_s": 3600.0,
    "time_step_s": 1.0,
    "graphic_period": 200,
    "friction_law": None,
    "friction_coefficient": None,
    "velocity_diffusivity": None,
    "tracer_diffusivity": None,
    "wind_speed_mps": 0.0,
    "wind_dir_from_deg": 0.0,
    "wind_drag_coef": None,
    "rain_or_evap_mm_per_day": None,
    # WAQTEL first-order tracer degradation (process 17).
    "decay_law": 1,
    "decay_coef": 2.0,
    # WAQTEL O2 (process 2) - the dissolved-oxygen sag below a discharge.
    "do_sag_bod_mgl": 20.0,
    "do_sag_upstream_do_mgl": 9.0,
    "do_sat_mgl": 9.0,
    "do_water_temp_c": 20.0,
    "do_k1_per_day": 0.3,
    "do_k2_per_day": 0.9,
    "do_k2_formula": 0,
    # GAIA sediment.
    "sediment_type": "sand",
    "sediment_density": 2650.0,
    "sediment_gradation": (),
    "grain_size_um": 200.0,
    "bed_thickness_m": 5.0,
    "bedload_formula": 1,
    "morphological_factor": 10.0,
    # NESTOR dredging.
    "dredge_mode": "scheduled",
    "dredge_station_frac": 0.5,
    "dredge_disposal": False,
    "dredge_disposal_station_frac": 0.85,
    "dredge_volume_m3": 4000.0,
    "dredge_start_frac": 0.15,
    "dredge_end_frac": 0.95,
    "dredge_crit_depth_m": 0.3,
    "dredge_dig_depth_m": 1.5,
    "dredge_rate_m_per_s": 5.0e-4,
    "dredge_design_grade_m": None,
    "dredge_zone_utm": (),
    "disposal_zone_utm": (),
    "dredge_zone_len_m": None,
    # Oil.
    "oil_preset": "light_crude",
    "oil_release_step": 600,
    "n_drogues": 100,
    "drogues_period_s": 60,
    # Rain on grid.
    "amc_condition": 2,
    "initial_abstraction_option": 1,
    "rain_duration_s": None,
}


class DeckAuthorError(RuntimeError):
    """A deck could not be authored; carries an open-set ``error_code``."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class _Sheet:
    """Attribute view over the deck mapping, falling through to the defaults.

    The writers below read the sheet by name. Reading through one view keeps the
    deck a plain serializable mapping - the thing the run record IS - while the
    writers stay readable as the keyword statements they produce.
    """

    def __init__(self, deck: Mapping[str, Any]) -> None:
        self._deck = dict(deck or {})

    def __getattr__(self, name: str) -> Any:
        if name in self._deck:
            return self._deck[name]
        if name in _DEFAULTS:
            return _DEFAULTS[name]
        raise AttributeError(name)


def _cas_real(value: float) -> str:
    """A REAL as DAMOCLES reads one: an integer-valued float keeps its point."""
    text = f"{float(value):g}"
    return text if any(c in text for c in ".eE") else text + "."


def _write_lines(rundir: Path | str, basename: str,
                 lines: Sequence[str]) -> str:
    """Write ``lines`` into ``rundir`` -> the basename written."""
    Path(rundir, basename).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return basename


def _write_deck(rundir: Path | str, basename: str,
                lines: Sequence[str]) -> str:
    """Write a DAMOCLES-parsed deck, every line inside the character limit.

    One over-long line does not fail where it is: the parser runs on and blames a
    later, valid keyword, so the clamp is applied to the whole file rather than
    trusted to the keyword lengths.
    """
    return _write_lines(rundir, basename,
                        [ln[:_CAS_LINE_LIMIT] for ln in lines])


# --------------------------------------------------------------------------- #
# The TELEMAC-2D reach deck and the files it names.
# --------------------------------------------------------------------------- #
def author_reach_deck(rundir: Path | str, *, deck: Mapping[str, Any],
                      geometry: str, boundary: str, results: str,
                      cas_name: str, liquid_boundary_order: Sequence[str],
                      bed: Mapping[str, Any], source_utm: tuple[float, float],
                      centerline_utm: Any = None,
                      dredge_zone_width_m: float | None = None,
                      node_xy: Any = None, node_bed: Any = None
                      ) -> dict[str, Any]:
    """Write the reach ``.cas`` and every file it names -> what was written.

    Clean flow drives the reach - a flowrate in at the inflow, a stage at the
    outflow, zero tracer at both - and the substance enters at a mid-reach POINT
    SOURCE for a finite window, so the plume advects downstream and dilutes
    instead of saturating the domain. ``source_utm`` is where that source sits:
    the settled release point, in the mesh's own metres.

    ``liquid_boundary_order`` is the MEASURED order the pair writer reported, and
    the PRESCRIBED lists are written in it. ``bed`` is the fitted-bed record the
    outflow stage is read from.
    """
    P = _Sheet(deck)
    rundir = Path(rundir)
    substance = str(getattr(P, "substance_class", "tracer")).lower()
    is_do_sag = substance == "do_sag"
    written: dict[str, Any] = {}

    outflow_stage = float(bed["bed_top_m"]) - float(bed["bed_drop_m"]) \
        + float(P.init_depth_m)
    flowrates, elevations, tracers = [], [], []
    for role in liquid_boundary_order:
        if role == "inflow":
            flowrates.append(f"{P.inflow_q_m3s}")
            elevations.append("0.0")
            if is_do_sag:
                # WAQTEL O2 appends DISSOLVED O2, ORGANIC LOAD and NH4 LOAD after
                # the dye, boundary-major in the module's own tracer order: the
                # fully-mixed discharge rides in here and the dye stays clean.
                tracers += ["0.0", f"{float(P.do_sag_upstream_do_mgl):g}",
                            f"{float(P.do_sag_bod_mgl):g}", "0.0"]
            else:
                tracers.append("0.0")
        else:
            flowrates.append("0.0")
            elevations.append(f"{outflow_stage:.3f}")
            tracers += ["0.0", "0.0", "0.0", "0.0"] if is_do_sag else ["0.0"]

    sx, sy = float(source_utm[0]), float(source_utm[1])
    # do_sag models the reach STARTING at the fully-mixed discharge - the CBOD and
    # DO ride in at the inflow boundary - so there is no point-source pulse, and a
    # single-tracer source array would collide with the O2 module's four tracers.
    if is_do_sag:
        sources_file_line = ""
        sources_block = ""
    else:
        written["sources"] = write_sources_pulse(rundir, deck=deck)
        sources_file_line = f"SOURCES FILE                    = {SOURCES_FILENAME}\n"
        sources_block = (
            "MAXIMUM NUMBER OF SOURCES        = 20\n"
            f"ABSCISSAE OF SOURCES             = {sx:.3f}\n"
            f"ORDINATES OF SOURCES             = {sy:.3f}\n"
            "WATER DISCHARGE OF SOURCES       = 0.0\n"
            "VALUES OF THE TRACERS AT THE SOURCES = 0.0\n"
        )

    friction_law = 3 if P.friction_law is None else int(P.friction_law)
    friction_coef = ("33." if P.friction_coefficient is None
                     else _cas_real(P.friction_coefficient))
    velocity_diff = ("1.E-1" if P.velocity_diffusivity is None
                     else _cas_real(P.velocity_diffusivity))
    tracer_diff = ("1.E-1" if P.tracer_diffusivity is None
                   else _cas_real(P.tracer_diffusivity))

    # WIND STRESS. The meteorological FROM-direction becomes velocity components
    # pointing where the wind BLOWS TOWARD, in the mesh's UTM frame: wind from the
    # north drives water southward, wind from the west drives it eastward.
    wind_speed = float(getattr(P, "wind_speed_mps", 0.0) or 0.0)
    if wind_speed > 0.0:
        theta = math.radians(float(getattr(P, "wind_dir_from_deg", 0.0) or 0.0))
        drag = getattr(P, "wind_drag_coef", None)
        drag_line = ("" if drag is None else
                     f"COEFFICIENT OF WIND INFLUENCE   = {_cas_real(drag)}\n")
        wind_block = (
            "WIND                            = YES\n"
            "OPTION FOR WIND                 = 1\n"
            f"{drag_line}"
            f"WIND VELOCITY ALONG X           = "
            f"{_cas_real(-wind_speed * math.sin(theta))}\n"
            f"WIND VELOCITY ALONG Y           = "
            f"{_cas_real(-wind_speed * math.cos(theta))}\n"
            "THRESHOLD DEPTH FOR WIND        = 1.\n")
    else:
        wind_block = ""

    # DISTRIBUTED RAIN / EVAPORATION - the native source term at every wet node,
    # independent of the inflow hydrograph. Signed: positive rains, negative
    # evaporates. With tracers present DAMOCLES REQUIRES a rainwater concentration
    # per tracer, and rainwater carries none of them.
    rain_rate = getattr(P, "rain_or_evap_mm_per_day", None)
    if rain_rate is not None:
        suspended = substance == "sediment" and not bool(P.erodible_bed)
        n_tracers = 4 if is_do_sag else (2 if suspended else 1)
        rain_block = (
            "RAIN OR EVAPORATION             = YES\n"
            f"RAIN OR EVAPORATION IN MM PER DAY = {_cas_real(float(rain_rate))}\n"
            f"VALUES OF TRACERS IN THE RAIN   = {';'.join(['0.'] * n_tracers)}\n")
    else:
        rain_block = ""

    graphic_variables = "'U,V,H,S,B,T1'"
    initial_tracers = "0."
    if is_do_sag:
        graphic_variables = "'U,V,H,S,B,T1,T2,T3,T4'"
        initial_tracers = f"0.;{float(P.do_sag_upstream_do_mgl):g};0.;0."
    elif substance == "sediment" and not bool(P.erodible_bed):
        # GAIA's suspended class arrives as a SECOND t2d tracer: the deck must
        # output it, and PRESCRIBED TRACERS VALUES must cover both tracers on
        # every liquid boundary or the solver refuses for want of values.
        graphic_variables = "'U,V,H,S,B,T1,T2'"
        tracers = ["0."] * (2 * max(len(liquid_boundary_order), 1))

    cas = f"""/-------------------------------------------------------------------/
/  TELEMAC-2D REACH  -  {P.name}
/  Clean flow drives the reach; a finite pulse is released at a
/  mid-reach point source (x={sx:.1f} y={sy:.1f}) for
/  {float(P.pulse_window_s):.0f}s, so the plume advects and dilutes.
/  Measured liquid-boundary order: {list(liquid_boundary_order)}
/-------------------------------------------------------------------/
GEOMETRY FILE                   = {os.path.basename(geometry)}
BOUNDARY CONDITIONS FILE        = {os.path.basename(boundary)}
RESULTS FILE                    = {os.path.basename(results)}
{sources_file_line}/
TITLE : '{P.name} REACH'
VARIABLES FOR GRAPHIC PRINTOUTS = {graphic_variables}
GRAPHIC PRINTOUT PERIOD         = {P.graphic_period}
LISTING PRINTOUT PERIOD         = 500
/
DURATION                        = {P.duration_s}
TIME STEP                       = {P.time_step_s}
/
INITIAL CONDITIONS              = 'CONSTANT DEPTH'
INITIAL DEPTH                   = {float(P.init_depth_m):.3f}
/
PRESCRIBED FLOWRATES            = {';'.join(flowrates)}
PRESCRIBED ELEVATIONS           = {';'.join(elevations)}
/
{sources_block}/
LAW OF BOTTOM FRICTION          = {friction_law}
FRICTION COEFFICIENT            = {friction_coef}
VELOCITY DIFFUSIVITY            = {velocity_diff}
{wind_block}{rain_block}/
EQUATIONS                       = 'SAINT-VENANT FE'
TREATMENT OF THE LINEAR SYSTEM  = 2
TYPE OF ADVECTION               = 1;5
SUPG OPTION                     = 0;0
MASS-LUMPING ON H : 1.
CONTINUITY CORRECTION : YES
SOLVER                          = 1
SOLVER ACCURACY                 = 1.E-6
MAXIMUM NUMBER OF ITERATIONS FOR SOLVER = 500
IMPLICITATION FOR DEPTH         = 0.6
IMPLICITATION FOR VELOCITY      = 0.6
TIDAL FLATS                             = YES
OPTION FOR THE TREATMENT OF TIDAL FLATS = 1
TREATMENT OF NEGATIVE DEPTHS            = 2
H CLIPPING     : NO
/
NUMBER OF TRACERS               = 1
NAMES OF TRACERS                = 'DYE             MG/L'
INITIAL VALUES OF TRACERS       = {initial_tracers}
PRESCRIBED TRACERS VALUES       = {';'.join(tracers)}
SCHEME FOR ADVECTION OF TRACERS          = 1
COEFFICIENT FOR DIFFUSION OF TRACERS     = {tracer_diff}
"""

    if substance == "oil":
        # The oil module rides ON TOP of the tracer solve and the steering file's
        # presence activates it. Floats released in shallow margins or against a
        # wall are dropped from the drogues tracker, so the release the module
        # gets is the clearance-snapped point the caller settled.
        written.update(write_oil_inputs(rundir, deck=deck, x=sx, y=sy))
        cas += (
            "/\n"
            "FORTRAN FILE                    = user_fortran\n"
            "OIL SPILL STEERING FILE         = oil_spill.txt\n"
            f"MAXIMUM NUMBER OF DROGUES       = {int(P.n_drogues)}\n"
            "PRINTOUT PERIOD FOR DROGUES     = "
            f"{max(int(float(P.drogues_period_s) / max(float(P.time_step_s), 1e-6)), 1)}\n"
            "ASCII DROGUES FILE              = drogues.txt\n")

    if substance == "decay":
        # Process 17's nametrac branch applies a decay SINK to every existing user
        # tracer, so it rides on the unchanged dye: zero new tracers, and the law
        # and its coefficient live in the steering file.
        written["waqtel"] = write_waqtel_decay(rundir, deck=deck)
        cas += ("/\n"
                "COUPLING WITH                   = 'WAQTEL'\n"
                f"WAQTEL STEERING FILE            = {WAQTEL_FILENAME}\n"
                "WATER QUALITY PROCESS           = 17\n")

    if is_do_sag:
        written["waqtel"] = write_waqtel_o2(rundir, deck=deck)
        cas += ("/\n"
                "COUPLING WITH                   = 'WAQTEL'\n"
                f"WAQTEL STEERING FILE            = {WAQTEL_FILENAME}\n"
                "WATER QUALITY PROCESS           = 2\n")

    if substance == "sediment":
        written["gaia"] = write_gaia_deck(rundir, deck=deck, geometry=geometry,
                                          boundary=boundary)
        cas += ("/\n"
                "COUPLING WITH                   = 'GAIA'\n"
                f"GAIA STEERING FILE              = {GAIA_STEERING_FILENAME}\n")
        if bool(P.dredging):
            written["nestor"] = write_nestor_decks(
                rundir, deck=deck, centerline_utm=centerline_utm,
                zone_width_m=dredge_zone_width_m,
                node_xy=node_xy, node_bed=node_bed)
            year, month, day, hour, minute, second = NESTOR_TIME_ORIGIN
            cas += (f"ORIGINAL DATE OF TIME           = {year};{month};{day}\n"
                    f"ORIGINAL HOUR OF TIME           = {hour};{minute};{second}\n")

    # A quoted TITLE that runs long is shortened keeping its quotes; a comment is
    # sliced. Data lines are authored short by construction.
    lines = []
    for line in cas.splitlines():
        if len(line) <= _CAS_LINE_LIMIT or line.startswith("/"):
            lines.append(line[:_CAS_LINE_LIMIT])
        elif line.startswith("TITLE"):
            lines.append(f"TITLE : '{str(P.name)[:40]} REACH'"[:_CAS_LINE_LIMIT])
        else:
            lines.append(line)
    Path(rundir, cas_name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    written["cas"] = cas_name
    logger.info("reach deck authored: %s substance=%s lb_order=%s -> %s",
                cas_name, substance, list(liquid_boundary_order), sorted(written))
    return written


def write_sources_pulse(rundir: Path | str, *,
                        deck: Mapping[str, Any]) -> str:
    """The SOURCES FILE: a finite pulse, then the point source stops.

    Columns are TELEMAC-2D's own source names - ``T`` (s), ``Q(1)`` (m3/s carrier
    discharge), ``TR(1,1)`` (concentration). The carrier and the substance are
    held over the pulse window then step to zero, so the slug travels downstream
    and passes. The final time runs past DURATION so the time interpolation never
    reads off the end of the series.
    """
    P = _Sheet(deck)
    window = float(P.pulse_window_s)
    discharge = float(P.source_q_m3s)
    concentration = float(P.dye_conc_mgl)
    end = max(float(P.duration_s) + 100.0, window + 100.0)
    return _write_deck(rundir, SOURCES_FILENAME, [
        "#", "T Q(1) TR(1,1)", "s m3/s mg/l",
        f"0.0 {discharge:.3f} {concentration:.3f}",
        f"{window:.3f} {discharge:.3f} {concentration:.3f}",
        f"{window + 0.1:.3f} 0.0 0.0",
        f"{end:.3f} 0.0 0.0"])


def write_waqtel_decay(rundir: Path | str, *, deck: Mapping[str, Any]) -> str:
    """The WAQTEL steering for first-order tracer DEGRADATION (process 17).

    Two keys, sized to the one user tracer: the law (1 = T90 die-off in hours,
    2 = first-order k per hour, 3 = per day) and its coefficient.
    """
    P = _Sheet(deck)
    law = int(getattr(P, "decay_law", 1))
    coef = float(getattr(P, "decay_coef", 2.0))
    return _write_deck(rundir, WAQTEL_FILENAME, [
        "/------------------------------------------------------------------/",
        "/  WAQTEL steering - first-order tracer DEGRADATION (process 17)",
        f"/  law={law} (1=T90 h, 2=k h^-1, 3=k d^-1)  coef={coef:g}  ntrac=1",
        "/------------------------------------------------------------------/",
        f"LAW OF TRACERS DEGRADATION           = {law}",
        f"COEFFICIENT 1 FOR LAW OF TRACERS DEGRADATION = {coef:g}"])


def write_waqtel_o2(rundir: Path | str, *, deck: Mapping[str, Any]) -> str:
    """The WAQTEL steering for the O2 module (process 2) - the sag kinetics.

    The eutrophication and benthic oxygen sources are zeroed and nitrification is
    off, leaving first-order deoxygenation balanced by surface reaeration - the
    Streeter-Phelps pair. A zero formula for K2 uses the constant reaeration
    coefficient rather than computing one from the modeled velocity and depth,
    and a zero formula for CS uses the constant saturation given.
    """
    P = _Sheet(deck)
    k1 = float(getattr(P, "do_k1_per_day", 0.3))
    k2 = float(getattr(P, "do_k2_per_day", 0.9))
    formk2 = int(getattr(P, "do_k2_formula", 0))
    saturation = float(getattr(P, "do_sat_mgl", 9.0))
    temperature = float(getattr(P, "do_water_temp_c", 20.0))
    return _write_deck(rundir, WAQTEL_FILENAME, [
        "/------------------------------------------------------------------/",
        "/  WAQTEL O2 steering - dissolved-oxygen sag (process 2)",
        f"/  k1={k1:g} k2={k2:g} (FORMK2={formk2}) Cs={saturation:g} "
        f"T={temperature:g}C",
        "/------------------------------------------------------------------/",
        f"WATER TEMPERATURE                             = {temperature:g}",
        "WATER SALINITY                                = 0.",
        f"CONSTANT OF DEGRADATION OF ORGANIC LOAD K1    = {k1:g}",
        "CONSTANT OF NITRIFICATION KINETIC K4          = 0.",
        f"FORMULA FOR COMPUTING K2                      = {formk2}",
        f"K2 REAERATION COEFFICIENT                     = {k2:g}",
        "FORMULA FOR COMPUTING CS                      = 0",
        f"O2 SATURATION DENSITY OF WATER (CS)           = {saturation:g}",
        "BENTHIC DEMAND                                = 0.",
        "PHOTOSYNTHESIS P                              = 0.",
        "VEGETAL RESPIRATION R                         = 0."])


def _normalize_gradation(raw: Any) -> list[tuple[float, float]]:
    """A gradation spec -> a clean fine-to-coarse ``[(d50_um, fraction), ...]``.

    Each diameter is held inside the silt-to-coarse-sand band the single-class
    path covers; fractions are renormalized so a caller can pass raw weights.
    Fewer than two surviving classes is not a mixture and returns nothing, so the
    caller keeps its single-class path.
    """
    out: list[tuple[float, float]] = []
    for item in (raw or ()):
        try:
            if isinstance(item, dict):
                micron = float(item.get("d50_um"))
                fraction = float(item.get("fraction", 0.0))
            else:
                micron, fraction = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if not (micron > 0.0) or fraction < 0.0:
            continue
        out.append((min(max(micron, 5.0), 2000.0), fraction))
    if len(out) < 2:
        return []
    out.sort(key=lambda pair: pair[0])
    out = out[:6]
    total = sum(fraction for _, fraction in out)
    if total <= 0.0:
        equal = 1.0 / len(out)
        return [(micron, equal) for micron, _ in out]
    return [(micron, fraction / total) for micron, fraction in out]


def write_gaia_deck(rundir: Path | str, *, deck: Mapping[str, Any],
                    geometry: str, boundary: str) -> str:
    """The GAIA steering, in one of the three shapes the sheet selects.

    GAIA reads the SAME geometry and boundary files as the hydrodynamics and
    writes its own results. Which shape it takes is what was asked for:

      * a MIXTURE - several non-cohesive classes over one erodible bed, coupled
        by a hiding factor, so the bed SORTS under a flood: fines winnow out of
        the high-shear thalweg and settle in slack water;
      * an ERODIBLE BED - one class, bedload on, a real sediment stock, so the
        bed scours where the flow steepens and re-deposits where it slackens; or
      * a SUPPLY-LIMITED SUSPENSION - one settling class over a bed with no stock
        at all, so nothing erodes and only what was injected deposits.

    Both bedload shapes keep suspension OFF, which is also what keeps the dye the
    sole hydrodynamic tracer; the suspension shape appends its class as a second
    one. Cohesive sediment is approximated as very fine non-cohesive - the
    Krone/Partheniades path is not emitted.
    """
    P = _Sheet(deck)
    concentration = max(float(getattr(P, "dye_conc_mgl", 100.0)) / 1000.0, 0.0)
    d50_m = max(float(getattr(P, "grain_size_um", 200.0)), 1.0) * 1.0e-6
    density = float(getattr(P, "sediment_density", 2650.0))
    gradation = _normalize_gradation(getattr(P, "sediment_gradation", ()))
    dredging = bool(getattr(P, "dredging", False))
    head = [
        f"GEOMETRY FILE                   = {os.path.basename(geometry)}",
        f"BOUNDARY CONDITIONS FILE        = {os.path.basename(boundary)}",
        f"RESULTS FILE                    = {GAIA_RESULT_FILENAME}"]

    if len(gradation) >= 2:
        thickness = max(float(getattr(P, "bed_thickness_m", 5.0)), 0.01)
        formula = int(getattr(P, "bedload_formula", 1) or 1)
        factor = max(float(getattr(P, "morphological_factor", 10.0)), 1.0)
        lines = [
            "/------------------------------------------------------------------/",
            "/  GAIA steering - MULTI-CLASS GRADED bedload (grain sorting)",
            f"/  {len(gradation)} classes, Egiazaroff hiding",
            "/------------------------------------------------------------------/",
            *head,
            "VARIABLES FOR GRAPHIC PRINTOUTS = 'B,E,D50'",
            "CLASSES TYPE OF SEDIMENT        = "
            + ";".join("NCO" for _ in gradation),
            "CLASSES SEDIMENT DIAMETERS      = "
            + ";".join(f"{um * 1.0e-6:g}" for um, _ in gradation),
            "CLASSES SEDIMENT DENSITY        = "
            + ";".join(f"{density:g}" for _ in gradation),
            "CLASSES INITIAL FRACTION        = "
            + ";".join(f"{fr:g}" for _, fr in gradation),
            "SUSPENSION FOR ALL SANDS        = NO",
            "BED LOAD FOR ALL SANDS          = YES",
            f"BED-LOAD TRANSPORT FORMULA FOR ALL SANDS = {formula}",
            "HIDING FACTOR FORMULA           = 1",
            f"LAYERS INITIAL THICKNESS        = {thickness:g}",
            f"MORPHOLOGICAL FACTOR            = {factor:g}",
            "MASS-BALANCE                    = YES"]
    elif bool(getattr(P, "erodible_bed", False)) or dredging:
        thickness = max(float(getattr(P, "bed_thickness_m", 5.0)), 0.01)
        formula = int(getattr(P, "bedload_formula", 1) or 1)
        factor = max(float(getattr(P, "morphological_factor", 10.0)), 1.0)
        lines = [
            "/------------------------------------------------------------------/",
            "/  GAIA steering - ERODIBLE BED, bedload morphodynamics (scour)",
            f"/  d50={d50_m * 1e6:g}um bed={thickness:g}m icf={formula} "
            f"mofac={factor:g}",
            "/------------------------------------------------------------------/",
            *head,
            "VARIABLES FOR GRAPHIC PRINTOUTS = 'B,E'",
            "CLASSES TYPE OF SEDIMENT        = NCO",
            f"CLASSES SEDIMENT DIAMETERS      = {d50_m:g}",
            f"CLASSES SEDIMENT DENSITY        = {density:g}",
            "CLASSES INITIAL FRACTION        = 1.",
            "SUSPENSION FOR ALL SANDS        = NO",
            "BED LOAD FOR ALL SANDS          = YES",
            f"BED-LOAD TRANSPORT FORMULA FOR ALL SANDS = {formula}",
            f"LAYERS INITIAL THICKNESS        = {thickness:g}",
            f"MORPHOLOGICAL FACTOR            = {factor:g}",
            "MASS-BALANCE                    = YES"]
    else:
        lines = [
            "/------------------------------------------------------------------/",
            "/  GAIA steering - ONE suspended NCO class, supply-limited bed",
            "/  (zero initial thickness -> only the injected pulse deposits)",
            f"/  d50={d50_m * 1e6:g}um conc={concentration:g}kg/m3",
            "/------------------------------------------------------------------/",
            *head,
            "VARIABLES FOR GRAPHIC PRINTOUTS = 'B,E'",
            "CLASSES TYPE OF SEDIMENT        = NCO",
            f"CLASSES SEDIMENT DIAMETERS      = {d50_m:g}",
            f"CLASSES SEDIMENT DENSITY        = {density:g}",
            "CLASSES INITIAL FRACTION        = 1.",
            "CLASSES SETTLING VELOCITIES     = -9.",
            "SUSPENSION FOR ALL SANDS        = YES",
            "BED LOAD FOR ALL SANDS          = NO",
            "SUSPENSION TRANSPORT FORMULA FOR ALL SANDS = 3",
            "LAYERS INITIAL THICKNESS        = 0.",
            "SCHEME FOR ADVECTION OF SUSPENDED SEDIMENTS = 1",
            "SUSPENDED SEDIMENTS CONCENTRATION VALUES AT THE SOURCES = "
            f"{concentration:g}",
            "MASS-BALANCE                    = YES"]

    if dredging:
        # The surface reference file is read on EVERY dig and dump - the node log
        # computes each node's chainage from it - so it is named in both modes,
        # not only by the criterion mode that also reads a design grade from it.
        nestor = [
            "/  NESTOR dredging (dig/dump on the erodible bed)",
            "NESTOR                          = YES",
            f"NESTOR ACTION FILE              = {NESTOR_ACTION_FILENAME}",
            f"NESTOR POLYGON FILE             = {NESTOR_POLYGON_FILENAME}",
            f"NESTOR SURFACE REFERENCE FILE   = {NESTOR_SURFACE_REF_FILENAME}"]
        lines = (lines[:-1] + nestor + [lines[-1]]
                 if lines and lines[-1].startswith("MASS-BALANCE")
                 else lines + nestor)
    return _write_deck(rundir, GAIA_STEERING_FILENAME, lines)


# --------------------------------------------------------------------------- #
# NESTOR: an action, the fields it acts on, and the grade it digs to.
# --------------------------------------------------------------------------- #
def _nestor_time(offset_s: float) -> str:
    """A sim-seconds offset as NESTOR's absolute ``yyyy.mm.dd-hh:mm:ss``."""
    base = datetime.datetime(*NESTOR_TIME_ORIGIN)
    return (base + datetime.timedelta(seconds=float(offset_s))
            ).strftime("%Y.%m.%d-%H:%M:%S")


def _channel_box(centerline: Any, station_frac: float, length_m: float,
                 width_m: float) -> list[tuple[float, float]]:
    """A channel-spanning rectangle around one centerline station.

    The corners are laid on the local along-channel tangent, so the box brackets
    the wetted section rather than sitting square to the grid.
    """
    import numpy as np

    line = np.asarray(centerline, dtype=float)
    arc = np.concatenate([[0.0], np.cumsum(
        np.hypot(*np.diff(line, axis=0).T))])
    total = float(arc[-1]) if arc[-1] > 0 else 1.0
    index = int(np.argmin(np.abs(arc - max(0.0, min(1.0, float(station_frac)))
                                 * total)))
    centre = line[index]
    tangent = line[min(index + 1, len(line) - 1)] - line[max(index - 1, 0)]
    norm = float(np.hypot(tangent[0], tangent[1]))
    unit = np.array([1.0, 0.0]) if norm < 1e-9 else tangent / norm
    perp = np.array([-unit[1], unit[0]])
    half_l, half_w = length_m / 2.0, width_m / 2.0
    return [(float(p[0]), float(p[1])) for p in (
        centre - half_l * unit - half_w * perp,
        centre + half_l * unit - half_w * perp,
        centre + half_l * unit + half_w * perp,
        centre - half_l * unit + half_w * perp)]


def _dredge_zones(P: _Sheet, centerline: Any, zone_width_m: float | None
                  ) -> tuple[list, list | None]:
    """The dig field and, when one was asked for, the dump field.

    An explicit polygon wins. Otherwise a channel-spanning box is laid at the
    stated station - and that needs a WIDTH the caller measured, because nothing
    here surveys a channel.
    """
    dig = list(getattr(P, "dredge_zone_utm", ()) or ())
    explicit_dump = list(getattr(P, "disposal_zone_utm", ()) or ())
    want_dump = bool(getattr(P, "dredge_disposal", False)) or len(explicit_dump) >= 3
    if len(dig) >= 3 and (not want_dump or len(explicit_dump) >= 3):
        return dig, (explicit_dump if want_dump else None)
    if zone_width_m is None or centerline is None:
        raise DeckAuthorError(
            "TELEMAC_DREDGE_ZONE_UNMEASURED",
            "a dredge field with no explicit polygon is laid across the channel, "
            "which needs the measured channel width and the centerline it is laid "
            "on; supply dredge_zone_utm/disposal_zone_utm, or the width.")
    width = float(zone_width_m) * 1.4
    stated = getattr(P, "dredge_zone_len_m", None)
    length = float(stated) if stated else 2.0 * float(zone_width_m)
    if len(dig) < 3:
        dig = _channel_box(centerline, getattr(P, "dredge_station_frac", 0.5),
                           length, width)
    dump = None
    if want_dump:
        dump = explicit_dump if len(explicit_dump) >= 3 else _channel_box(
            centerline, getattr(P, "dredge_disposal_station_frac", 0.85),
            length, width)
    return dig, dump


def _write_nestor_polygons(rundir: Path | str, dig: Any, dump: Any) -> str:
    """NESTOR's polygon file: a named block per field, then a bare terminator."""
    lines = [f"{_FILE_COMMENT}NESTOR polygon file - dredge/dump zones (UTM m)",
             f"NAME {_NESTOR_DIG_FIELD}"]
    lines += [f"{x:.3f} {y:.3f}" for x, y in dig]
    if dump:
        lines.append(f"NAME {_NESTOR_DUMP_FIELD}")
        lines += [f"{x:.3f} {y:.3f}" for x, y in dump]
    lines.append("ENDFILE")
    return _write_lines(rundir, NESTOR_POLYGON_FILENAME, lines)


def _write_nestor_action(rundir: Path | str, P: _Sheet, has_dump: bool) -> str:
    """NESTOR's action file - one action, in one of two modes.

    SCHEDULED digs a target volume over a window. BY CRITERION triggers wherever
    the silted bed rises within a tolerance of the design grade, digs down at a
    stated rate, and re-arms across the run so re-siltation is dredged again.
    """
    duration = float(getattr(P, "duration_s", 3600.0))
    mode = str(getattr(P, "dredge_mode", "scheduled")).lower()
    start = _nestor_time(max(0.0, float(getattr(P, "dredge_start_frac", 0.15)))
                         * duration)
    end = _nestor_time(min(1.0, float(getattr(P, "dredge_end_frac", 0.95)))
                       * duration)
    # RESTART is read as a Fortran LOGICAL, so it takes a Fortran literal and not
    # the DAMOCLES YES/NO the steering files use.
    lines = ["/ NESTOR action file - channel maintenance dredging",
             f"/ mode={mode}", "RESTART = F", "ACTION"]
    if mode == "criterion":
        rate = max(float(getattr(P, "dredge_rate_m_per_s", 5.0e-4)), 1.0e-9)
        lines += [
            "  ActionType      = Dig_by_criterion",
            f"  FieldDig        = {_NESTOR_DIG_FIELD}",
            f"  TimeStart       = {start}",
            f"  TimeEnd         = {end}",
            f"  TimeRepeat      = {max(duration / 4.0, 1.0):g}",
            f"  DigRate         = {rate:g}",
            f"  CritDepth       = {float(getattr(P, 'dredge_crit_depth_m', 0.3)):g}",
            f"  DigDepth        = {float(getattr(P, 'dredge_dig_depth_m', 1.5)):g}",
            "  MinVolume       = 0.",
            "  MinVolumeRadius = 0.",
            # SECTIONS interpolates the grade from the surface-reference profiles;
            # GRID would demand a gridded field that does not exist here.
            "  ReferenceLevel  = SECTIONS"]
        if has_dump:
            lines += [f"  FieldDump       = {_NESTOR_DUMP_FIELD}",
                      f"  DumpRate        = {rate:g}"]
    else:
        lines += [
            "  ActionType      = Dig_by_time",
            f"  FieldDig        = {_NESTOR_DIG_FIELD}",
            f"  TimeStart       = {start}",
            f"  TimeEnd         = {end}",
            f"  DigVolume       = "
            f"{max(float(getattr(P, 'dredge_volume_m3', 4000.0)), 1.0):g}"]
        if has_dump:
            # A dump field with no rate places the dug spoil over the same window.
            lines.append(f"  FieldDump       = {_NESTOR_DUMP_FIELD}")
    return _write_lines(rundir, NESTOR_ACTION_FILENAME,
                        lines + ["ENDACTION", "ENDFILE"])


def _write_nestor_surface_ref(rundir: Path | str, centerline: Any, *,
                              grade_m: float, half_width_m: float) -> str:
    """NESTOR's surface reference file - a fence of channel-crossing profiles.

    Every field node has to lie BETWEEN two profiles for its grade and chainage
    to interpolate, and consecutive profiles have to stay near-parallel, so the
    fence spans the whole reach at a spacing set by its own width and the end
    profiles are nudged past the ends to enclose the extreme nodes.
    """
    import numpy as np

    line = np.asarray(centerline, dtype=float)
    arc = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(line, axis=0).T))])
    total = float(arc[-1]) or 1.0
    step = max(int(len(line) // max(int(total / max(half_width_m, 1.0)) + 2, 3)), 1)
    indices = list(range(0, len(line), step))
    if indices[-1] != len(line) - 1:
        indices.append(len(line) - 1)
    lines = [f"{_FILE_COMMENT}NESTOR surface reference - design grade profiles"]
    for index in indices:
        centre = line[index]
        tangent = line[min(index + 1, len(line) - 1)] - line[max(index - 1, 0)]
        unit = tangent / (float(np.hypot(tangent[0], tangent[1])) or 1.0)
        perp = np.array([-unit[1], unit[0]])
        push = -5.0 if index == 0 else (5.0 if index == len(line) - 1 else 0.0)
        left = centre + push * unit - half_width_m * perp
        right = centre + push * unit + half_width_m * perp
        lines.append(f"{left[0]:.3f} {left[1]:.3f} {grade_m:.3f} "
                     f"{right[0]:.3f} {right[1]:.3f} {grade_m:.3f} "
                     f"{float(arc[index] / total):.5f}")
    return _write_lines(rundir, NESTOR_SURFACE_REF_FILENAME, lines + ["END"])


def write_nestor_decks(rundir: Path | str, *, deck: Mapping[str, Any],
                       centerline_utm: Any, zone_width_m: float | None,
                       node_xy: Any = None, node_bed: Any = None
                       ) -> dict[str, Any]:
    """Every NESTOR input for a dredging run -> what was written.

    The design grade is the mean bed over the dig field when the sheet states
    none - the grade a maintenance dredge digs back TO is the channel that is
    there, not a number invented for the deck.
    """
    P = _Sheet(deck)
    dig, dump = _dredge_zones(P, centerline_utm, zone_width_m)
    _write_nestor_polygons(rundir, dig, dump)
    _write_nestor_action(rundir, P, dump is not None)
    grade = getattr(P, "dredge_design_grade_m", None)
    if grade is None:
        grade = _mean_bed_over(dig, node_xy, node_bed)
    half_width = max(float(zone_width_m or 0.0) * 2.0, 30.0)
    _write_nestor_surface_ref(rundir, centerline_utm, grade_m=float(grade),
                              half_width_m=half_width)
    return {"action": NESTOR_ACTION_FILENAME, "polygon": NESTOR_POLYGON_FILENAME,
            "surface_ref": NESTOR_SURFACE_REF_FILENAME,
            "has_dump": dump is not None, "design_grade_m": float(grade)}


def _mean_bed_over(polygon: Any, node_xy: Any, node_bed: Any) -> float:
    """The mean bed inside ``polygon``, or over the whole mesh when none is in it."""
    import numpy as np
    from shapely.geometry import MultiPoint, Polygon
    from shapely.prepared import prep

    if node_xy is None or node_bed is None:
        return 0.0
    points = np.asarray(node_xy, dtype=float)
    bed = np.asarray(node_bed, dtype=float)
    field = prep(Polygon(np.asarray(polygon, dtype=float)))
    inside = np.array([field.covers(p) for p in MultiPoint(points).geoms])
    return float(np.mean(bed[inside])) if inside.any() else float(np.mean(bed))


# --------------------------------------------------------------------------- #
# The oil module's steering and its user fortran.
# --------------------------------------------------------------------------- #
#: Oil presets in the module reader's own format. Fractions sum to 1 per preset;
#: the aromatic rows carry mass fraction, boiling point, solubility and the
#: dissolution and volatilisation rates.
OIL_PRESETS: dict[str, dict[str, Any]] = {
    "light_crude": dict(
        compo=[(0.5, 645.0), (0.3, 830.0)],
        hap=[(0.2, 673.0, 0.018, 1.0e-5, 5.0e-5)],
        rho=850.0, eta=1.0e-5, voldev=20.0, tamb=288.0, etal=1),
    "diesel": dict(
        compo=[(0.6, 560.0), (0.25, 700.0)],
        hap=[(0.15, 610.0, 0.005, 1.0e-5, 8.0e-5)],
        rho=840.0, eta=4.0e-6, voldev=10.0, tamb=288.0, etal=1),
    "heavy_fuel": dict(
        compo=[(0.75, 900.0), (0.2, 1050.0)],
        hap=[(0.05, 800.0, 0.001, 5.0e-6, 1.0e-5)],
        rho=960.0, eta=5.0e-4, voldev=30.0, tamb=288.0, etal=1),
}

#: The oil user-fortran template ships beside this module: the deck names a
#: FORTRAN FILE and the release coordinates are compiled INTO it, so the file is
#: per-run and the run directory is where it is written.
_OIL_TEMPLATE_DIR = Path(__file__).resolve().parent / "oil_templates"


def write_oil_inputs(rundir: Path | str, *, deck: Mapping[str, Any],
                     x: float, y: float) -> dict[str, str]:
    """The oil steering file and the per-run ``oil_flot.f`` -> what was written.

    The template's release is the module's own demo; both the release step and
    the coordinates are rewritten to this run's, because a release the flow never
    reaches produces a clean run and an empty slick.
    """
    P = _Sheet(deck)
    preset_name = str(getattr(P, "oil_preset", "light_crude"))
    preset = OIL_PRESETS.get(preset_name, OIL_PRESETS["light_crude"])
    lines = [f"{preset_name.upper()} - trid3nt oil preset",
             str(len(preset["compo"])), "FM_COMPO TB_COMPO"]
    lines += [f"{fm} {tb}" for fm, tb in preset["compo"]]
    lines += ["NB_HAP", str(len(preset["hap"])),
              "FM_HAP TB_HAP SOLU KDISS KVOL"]
    lines += [" ".join(str(v) for v in row) for row in preset["hap"]]
    lines += ["RHO_OIL", str(preset["rho"]), "ETA_OIL", str(preset["eta"]),
              "VOLDEV", str(preset["voldev"]), "TAMB", str(preset["tamb"]),
              "ETAL", str(preset["etal"])]
    Path(rundir, "oil_spill.txt").write_text("\n".join(lines) + "\n",
                                             encoding="utf-8")

    template = (_OIL_TEMPLATE_DIR / "oil_flot_template.f").read_text(
        encoding="utf-8")
    template = template.replace(
        "IF(LT.EQ.60)", f"IF(LT.EQ.{int(getattr(P, 'oil_release_step', 600))})")
    template = re.sub(r"COORD_X=\d+\.D0", f"COORD_X={x:.0f}.D0", template)
    template = re.sub(r"COORD_Y=\d+\.D0", f"COORD_Y={y:.0f}.D0", template)
    fortran = Path(rundir, "user_fortran")
    fortran.mkdir(parents=True, exist_ok=True)
    (fortran / "oil_flot.f").write_text(template, encoding="utf-8")
    return {"steering": "oil_spill.txt", "fortran": "user_fortran/oil_flot.f"}


# --------------------------------------------------------------------------- #
# The rain-on-grid deck and the distributed fields it names.
# --------------------------------------------------------------------------- #
def write_cn_map(rundir: Path | str, basename: str, *, x: Any, y: Any,
                 cn2: Any) -> str:
    """The per-node curve-number scatter the engine interpolates onto the mesh.

    The scatter points ARE the mesh nodes, so the interpolation is an identity.
    """
    import numpy as np

    xs = np.asarray(x, dtype=float)
    ys = np.asarray(y, dtype=float)
    values = np.clip(np.asarray(cn2, dtype=float), 1.0, 100.0)
    if values.shape[0] != xs.shape[0]:
        raise DeckAuthorError(
            "TELEMAC_ROG_CN_LENGTH_MISMATCH",
            f"the curve-number field has {values.shape[0]} values and the mesh "
            f"has {xs.shape[0]} nodes.")
    return _write_lines(rundir, basename, [
        f"{_FILE_COMMENT}X Y CN2 (curve number, AMC-II)",
        *(f"{a:.3f} {b:.3f} {c:.3f}" for a, b, c in zip(xs, ys, values))])


def write_friction_files(rundir: Path | str, *, laws_basename: str,
                         zones_basename: str, manning_per_node: Any
                         ) -> dict[str, Any]:
    """The distributed-friction pair: the zone laws, and each node's zone.

    Distinct Manning values become zones. The laws file must be terminated or the
    scan reads past the end of it.
    """
    import numpy as np

    values = np.round(np.clip(np.asarray(manning_per_node, dtype=float),
                              0.005, 1.0), 3)
    unique = sorted(set(float(v) for v in values))
    zone_of = {v: i + 1 for i, v in enumerate(unique)}
    _write_lines(rundir, laws_basename, [
        "* rain-on-grid distributed Manning (per land cover)",
        "* zone  law      coef    vegetation",
        *(f"{zone_of[v]} MANNING {v:.3f} NULL" for v in unique),
        "END"])
    _write_lines(rundir, zones_basename, [
        f"{i} {zone_of[float(v)]}" for i, v in enumerate(values, start=1)])
    return {"n_zones": len(unique), "manning_values": unique}


def write_hyetograph_file(rundir: Path | str, basename: str, *, blocks: Any,
                          duration_s: float) -> dict[str, Any]:
    """The block hyetograph the time-varying rain branch reads.

    Each block is ``[t_end_s, gross_mm]`` over the interval since the previous
    one, which is the structure the engine's own reader consumes. A dry tail is
    appended past the last simulated instant so the reader never runs off the end
    at the final timestep.
    """
    rows: list[tuple[float, float]] = []
    previous = 0.0
    total = 0.0
    for t_end, millimetres in blocks:
        t_end, millimetres = float(t_end), float(millimetres)
        if t_end <= previous:
            raise DeckAuthorError(
                "TELEMAC_ROG_HYETO_NONMONOTONE",
                f"hyetograph times must strictly increase; got t_end={t_end} "
                f"after {previous}.")
        if millimetres < 0.0:
            raise DeckAuthorError(
                "TELEMAC_ROG_HYETO_NEGATIVE",
                f"hyetograph interval rainfall must be >= 0; got {millimetres} mm.")
        rows.append((t_end, millimetres))
        total += millimetres
        previous = t_end
    if not rows:
        raise DeckAuthorError("TELEMAC_ROG_HYETO_EMPTY",
                              "the hyetograph has no intervals.")
    tail = float(duration_s) + 3600.0
    if rows[-1][0] < tail:
        rows.append((tail, 0.0))
    _write_lines(rundir, basename, [
        f"{_FILE_COMMENT}HYETOGRAPH FILE (block type; mm per interval)",
        f"{_FILE_COMMENT}T (s) RAINFALL (mm)", "0.",
        *(f"{t:.3f} {mm:.5f}" for t, mm in rows)])
    return {"n_blocks": len(rows), "hyetograph_total_mm": round(total, 4)}


def author_rog_deck(rundir: Path | str, *, deck: Mapping[str, Any],
                    geometry: str, boundary: str, results: str, cas_name: str,
                    cn_map: str, friction_laws: str, zones_file: str,
                    rain_mm_per_day: float, runoff_path: str,
                    hyetograph_file: str | None = None,
                    user_fortran_dir: str | None = None) -> str:
    """Write the rain-on-grid ``.cas`` -> the basename written.

    Three rain paths, and the deck says which one it is:

      * CONSTANT NATIVE - a design-storm rate with the engine's own SCS-CN
        infiltration, optionally stopping before the run ends so the catchment
        drains and the recession limb appears;
      * TIME-VARYING NATIVE - the same infiltration applied per timestep to a
        real gross hyetograph read from a data file, which needs the per-run
        fortran that turns that branch on. The recession comes from the
        hyetograph's own dry tail, so no rain window is stated;
      * PRE-PROCESSED - the excess was computed before the run, so the engine's
        infiltration is off and the abstraction is not taken twice.

    All three share the free-exit outlet, the distributed Manning and the dry
    start. There are NO tracers: the outlet hydrograph is the product.
    """
    P = _Sheet(deck)
    amc = int(getattr(P, "amc_condition", 2) or 2)
    abstraction = int(getattr(P, "initial_abstraction_option", 1) or 1)
    duration_s = float(getattr(P, "duration_s", 3600.0))
    time_step = float(getattr(P, "time_step_s", 2.0))
    graphic = int(getattr(P, "graphic_period", 100))
    name = str(getattr(P, "name", "watershed"))

    rain_window = getattr(P, "rain_duration_s", None)
    window_line = ""
    if (hyetograph_file is None and rain_window is not None
            and 0.0 < float(rain_window) < duration_s):
        window_line = ("DURATION OF RAIN OR EVAPORATION IN HOURS = "
                       f"{_cas_real(float(rain_window) / 3600.0)}\n")
    rain_line = ("RAIN OR EVAPORATION             = YES\n"
                 "RAIN OR EVAPORATION IN MM PER DAY = "
                 f"{_cas_real(rain_mm_per_day)}\n")

    if hyetograph_file is not None:
        # The rate keyword stays because rain must be enabled, and is ignored:
        # the block file drives the intensity.
        runoff_block = (
            f"{rain_line}"
            "RAINFALL-RUNOFF MODEL           = 1\n"
            f"ANTECEDENT MOISTURE CONDITIONS  = {amc}\n"
            f"OPTION FOR INITIAL ABSTRACTION RATIO = {abstraction}\n"
            f"FORMATTED DATA FILE 2           = {os.path.basename(cn_map)}\n"
            "FORMATTED DATA FILE 1           = "
            f"{os.path.basename(hyetograph_file)}\n"
            f"FORTRAN FILE                    = {user_fortran_dir or 'user_fortran'}\n")
    elif str(runoff_path).lower() == "native":
        runoff_block = (
            f"{rain_line}{window_line}"
            "RAINFALL-RUNOFF MODEL           = 1\n"
            f"ANTECEDENT MOISTURE CONDITIONS  = {amc}\n"
            f"OPTION FOR INITIAL ABSTRACTION RATIO = {abstraction}\n"
            f"FORMATTED DATA FILE 2           = {os.path.basename(cn_map)}\n")
    else:
        runoff_block = (f"{rain_line}{window_line}"
                        "RAINFALL-RUNOFF MODEL           = 0\n")

    cas = f"""/-------------------------------------------------------------------/
/  TELEMAC-2D  RAIN-ON-GRID  -  {name}
/  Rain-fed catchment on a delineated watershed TIN (UTM metres).
/  Runoff path: {runoff_path}. Rain = {rain_mm_per_day:g} mm/day.
/  Distributed Manning; free-exit outlet at the pour point.
/-------------------------------------------------------------------/
GEOMETRY FILE                   = {os.path.basename(geometry)}
BOUNDARY CONDITIONS FILE        = {os.path.basename(boundary)}
RESULTS FILE                    = {os.path.basename(results)}
FRICTION DATA FILE              = {os.path.basename(friction_laws)}
ZONES FILE                      = {os.path.basename(zones_file)}
/
TITLE : '{name} RAIN-ON-GRID'
VARIABLES FOR GRAPHIC PRINTOUTS = 'U,V,H,S,B'
GRAPHIC PRINTOUT PERIOD         = {graphic}
LISTING PRINTOUT PERIOD         = {graphic}
/
DURATION                        = {duration_s}
TIME STEP                       = {time_step}
/
INITIAL CONDITIONS              = 'ZERO ELEVATION'
/
LAW OF BOTTOM FRICTION          = 4
FRICTION DATA                   = YES
{runoff_block}/
EQUATIONS                       = 'SAINT-VENANT FE'
TREATMENT OF THE LINEAR SYSTEM  = 2
TYPE OF ADVECTION               = 1;5
SUPG OPTION                     = 0;0
MASS-LUMPING ON H : 1.
CONTINUITY CORRECTION : YES
SOLVER                          = 1
SOLVER ACCURACY                 = 1.E-6
MAXIMUM NUMBER OF ITERATIONS FOR SOLVER = 200
IMPLICITATION FOR DEPTH         = 0.6
IMPLICITATION FOR VELOCITY      = 0.6
FREE SURFACE GRADIENT COMPATIBILITY = 0.9
TIDAL FLATS                             = YES
OPTION FOR THE TREATMENT OF TIDAL FLATS = 1
TREATMENT OF NEGATIVE DEPTHS            = 2
H CLIPPING     : NO
MASS-BALANCE                    = YES
INFORMATION ABOUT SOLVER        = YES
/
NUMBER OF TRACERS               = 0
"""
    Path(rundir, cas_name).write_text(cas, encoding="utf-8")
    logger.info("rain-on-grid deck authored: %s path=%s rain=%g mm/day",
                cas_name, runoff_path, rain_mm_per_day)
    return cas_name
