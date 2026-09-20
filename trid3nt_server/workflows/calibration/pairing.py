"""The pairing core: a model field and a record of observations, on four axes.

Space, time, VERTICAL DATUM and physical QUANTITY are reconciled and none is
guessed; every observation that does not survive comes back carrying its own
reason. This is what the observe slot coerces its record through - the SERIES
sibling of the single-value observation ingestion - so what it yields is Pairs
rather than a layer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import CalibrationError

__all__ = ["Pairs", "onto", "pairs", "observed_units", "quantity_of", "datum_shift"]

#: Observed fields whose NAME declares FEET, and those whose name declares or
#: implies METRES. A generic already-aligned name carries no unit tell and is
#: read as the model's own metre reference.
_FEET_FIELDS = frozenset({"elev_ft", "stage_ft", "gage_height_ft"})
_METRE_FIELDS = frozenset({"elev_m", "elev", "elevation", "head", "observed",
                           "observed_value", "obs_value", "value"})

#: Why an observation did not become a pair.
DROP_REASONS = ("outside_footprint", "nodata_sample", "unparseable_value",
                "no_time_match", "quantity_unreconciled")


@dataclass(frozen=True, slots=True)
class Pairs:
    """What the model and the record agreed on, and what they did not.

    ``observed`` and ``simulated`` run in step; ``dropped`` carries one row per
    observation that did not survive, each with its reason."""

    observed: tuple[float, ...] = ()
    simulated: tuple[float, ...] = ()
    times: tuple[str | None, ...] = ()
    ids: tuple[str | None, ...] = ()
    dropped: tuple[Mapping[str, Any], ...] = ()
    units: str = "m"
    quantity: str | None = None
    frame: str | None = None
    notes: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.observed)


def observed_units(field_name: str, stated: str | None) -> tuple[str, str]:
    """``(the unit the record is in, the note)`` for an observed field: what the
    caller stated, else what the field's own name declares.

    The unit only - the CONVERSION is the runtime's one function, so a record
    paired against a model goes through the same table a record ingested into a
    slot does. An undeterminable unit refuses: pairing a value of unknown unit
    against a metre raster is a silent feet-versus-metres error nothing
    downstream sees."""
    if stated:
        word = stated.strip().lower()
        if word in ("ft", "feet", "foot"):
            return "ft", f"{field_name} declared feet"
        if word in ("m", "meter", "meters", "metre", "metres"):
            return "m", f"{field_name} declared metres"
        return stated.strip(), f"{field_name} declared {stated.strip()}"
    name = field_name.strip().lower()
    if name in _FEET_FIELDS or name.endswith(("_ft", "_feet")):
        return "ft", f"{field_name} read as feet"
    if name in _METRE_FIELDS or name.endswith(("_m", "_metre", "_metres",
                                               "_meter", "_meters")):
        return "m", f"{field_name} read as metres"
    raise CalibrationError(
        "CALIBRATION_UNITS_UNKNOWN",
        f"the unit of the observed field {field_name!r} is not stated and its "
        "name does not declare one; the model is in metres, so pairing here "
        "would risk a silent feet-versus-metres mismatch. State the units.")


def onto(value: float, units: str, to_units: str) -> float:
    """One observed value on the unit the MODEL publishes, through the runtime's
    one conversion.

    The observe slot's ingestion reads the opening value through the same
    function, so a record converted for the journal and the same record
    converted for a residual are converted once, by one table."""
    from trid3nt_server.inputs.observation import ObservationError, convert

    try:
        return convert(value, units, to_units)
    except ObservationError as exc:
        raise CalibrationError("CALIBRATION_UNITS_UNKNOWN", str(exc)) from exc


def quantity_of(name: str | None) -> str | None:
    """``depth`` above ground, ``elevation`` above a datum, or None with no tell.

    Depth words test FIRST: a name carrying both means the depth."""
    word = (name or "").strip().lower()
    if not word:
        return None
    if any(k in word for k in ("depth", "height_above_gnd", "water_depth",
                               "hmax", "depth_above_ground")):
        return "depth"
    if any(k in word for k in ("water_surface_elevation", "wse",
                               "water_surface", "elev", "head")):
        return "elevation"
    return None


def datum_shift(observed_frame: str | None, model_frame: str | None,
                stated_m: float | None) -> tuple[float, str]:
    """``(metres to ADD to every observed value, what a reader must know)``.

    Two named frames that differ with no stated shift refuse: an unreconciled
    vertical shift makes every residual meaningless, and NAVD88 against NGVD29
    is a metre of error that looks like model bias."""
    if stated_m is not None:
        return float(stated_m), (
            f"observed values shifted {float(stated_m):+.4f} m from "
            f"{observed_frame or 'their own frame'} onto "
            f"{model_frame or 'the model frame'}, as stated.")
    left = (observed_frame or "").strip().upper()
    right = (model_frame or "").strip().upper()
    if left and right:
        if left == right:
            return 0.0, f"both sides count from {observed_frame}; no shift."
        raise CalibrationError(
            "CALIBRATION_DATUM_MISMATCH",
            f"the observations count from {observed_frame!r} and the model from "
            f"{model_frame!r}, and no shift between them was stated. An "
            "unreconciled vertical datum makes every residual meaningless: "
            "state the shift in metres (model minus observed).")
    return 0.0, (
        "one side or neither states a vertical frame, so a matching datum is "
        "assumed; the pairs stay valid for RELATIVE bias either way.")


def _number(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out if math.isfinite(out) else float("nan")


def _bilinear(band: Any, transform: Any, x: float, y: float) -> tuple[bool, float]:
    """``(inside the extent, the value there)``, bilinear between cell centres."""
    import numpy as np
    from scipy.ndimage import map_coordinates

    col, row = (~transform) * (x, y)
    height, width = band.shape
    inside = 0 <= col <= width and 0 <= row <= height
    sample = map_coordinates(band, np.array([[row - 0.5], [col - 0.5]]),
                             order=1, mode="constant", cval=np.nan)
    return inside, float(sample[0])


def _nearest_wet(band: Any, transform: Any, x: float, y: float,
                 radius_px: int) -> tuple[float, float]:
    """``(value, distance in cells)`` for the nearest finite cell within reach.

    A mark standing one cell outside the wet edge is a real measurement of a
    real flood; reading it as nodata would drop the observation that matters
    most. Ties go to the first in row-major order."""
    import numpy as np

    col, row = (~transform) * (x, y)
    ci, ri = int(math.floor(col)), int(math.floor(row))
    height, width = band.shape
    r0, r1 = max(0, ri - radius_px), min(height, ri + radius_px + 1)
    c0, c1 = max(0, ci - radius_px), min(width, ci + radius_px + 1)
    if r0 >= r1 or c0 >= c1:
        return float("nan"), float("inf")
    window = band[r0:r1, c0:c1]
    dr = (np.arange(r0, r1, dtype=float) + 0.5 - row)[:, None]
    dc = (np.arange(c0, c1, dtype=float) + 0.5 - col)[None, :]
    distance = np.where(np.isfinite(window), np.hypot(dc, dr), np.inf)
    flat = int(np.argmin(distance))
    if not math.isfinite(float(distance.flat[flat])):
        return float("nan"), float("inf")
    wr, wc = divmod(flat, c1 - c0)
    return float(window.flat[flat]), math.hypot((c0 + wc + 0.5) - col,
                                                (r0 + wr + 0.5) - row)


def _field(band: Any) -> Any:
    """Nodata as NaN, so every sampler below reads one missing value."""
    import numpy as np

    return np.asarray(band, dtype=np.float64)


def pairs(model: Any, observed: Sequence[Mapping[str, Any]], *,
          value_field: str = "value", units: str | None = None,
          to_units: str = "m", frame: str | None = None,
          shift_m: float | None = None, quantity: str | None = None,
          wet_reach_m: float = 250.0, ground: Any = None) -> Pairs:
    """The model read at every observation that survives the four axes.

    ``model`` is an open rasterio dataset or a raster path; ``observed`` is the
    record, each row carrying ``lon``/``lat``, the measured value under
    ``value_field``, and optionally ``time``, ``id``, ``vertical_datum`` and
    ``quantity``. ``to_units`` is the unit the MODEL publishes the variable in -
    the one the record is converted onto - and ``ground`` is a ground-elevation
    raster, the only thing that reconciles a model DEPTH against an observed
    ELEVATION."""
    import numpy as np
    import rasterio

    opened = None
    if isinstance(model, (str, bytes)):
        opened = rasterio.open(model)
        source = opened
    else:
        source = model
    try:
        band = _field(source.read(1))
        nodata = source.nodata
        if nodata is not None and math.isfinite(float(nodata)) \
                and not math.isnan(float(nodata)):
            band[band == float(nodata)] = np.nan
        transform = source.transform
        tags = dict(source.tags() or {})
        model_frame = frame or tags.get("vertical_datum")
        model_quantity = quantity_of(quantity or tags.get("quantity")
                                     or str(getattr(source, "name", "") or ""))
        pixel_m = max(abs(transform.a), abs(transform.e)) or 1.0
    finally:
        if opened is not None:
            opened.close()

    if not observed:
        raise CalibrationError(
            "CALIBRATION_NO_OBSERVATIONS",
            "the record carries no observations, so there is nothing to pair "
            "the model against.")

    record_units, unit_note = observed_units(value_field, units)
    frames = sorted({str(row.get("vertical_datum") or "").strip()
                     for row in observed} - {""})
    shift, datum_note = datum_shift(frames[0] if len(frames) == 1 else None,
                                    model_frame, shift_m)
    obs_quantity = quantity_of(
        next((str(row.get("quantity")) for row in observed
              if row.get("quantity")), None) or value_field)
    if (model_quantity and obs_quantity and model_quantity != obs_quantity
            and ground is None):
        raise CalibrationError(
            "CALIBRATION_QUANTITY_MISMATCH",
            f"the model measures {model_quantity} and the record measures "
            f"{obs_quantity}; their difference is dominated by ground elevation, "
            "so a residual across the two measures the terrain rather than the "
            "model. Supply a ground-elevation surface to convert through.")

    radius_px = max(0, int(round(float(wet_reach_m) / pixel_m)))
    kept_obs: list[float] = []
    kept_sim: list[float] = []
    kept_times: list[str | None] = []
    kept_ids: list[str | None] = []
    dropped: list[Mapping[str, Any]] = []
    for index, row in enumerate(observed):
        identity = row.get("id") if row.get("id") is not None else index
        value = _number(row.get(value_field))
        if not math.isfinite(value):
            dropped.append({"id": identity, "reason": "unparseable_value"})
            continue
        x, y = _number(row.get("lon")), _number(row.get("lat"))
        if not (math.isfinite(x) and math.isfinite(y)):
            dropped.append({"id": identity, "reason": "unparseable_value"})
            continue
        inside, sample = _bilinear(band, transform, x, y)
        if not inside:
            dropped.append({"id": identity, "reason": "outside_footprint"})
            continue
        if not math.isfinite(sample) and radius_px:
            sample, _cells = _nearest_wet(band, transform, x, y, radius_px)
        if not math.isfinite(sample):
            dropped.append({"id": identity, "reason": "nodata_sample"})
            continue
        if model_quantity == "depth" and obs_quantity == "elevation":
            _inside, ground_z = _bilinear(_field(ground.read(1)),
                                          ground.transform, x, y)
            if not math.isfinite(ground_z):
                dropped.append({"id": identity, "reason": "quantity_unreconciled"})
                continue
            sample = sample + ground_z
        kept_obs.append(onto(value, record_units, to_units) + shift)
        kept_sim.append(sample)
        kept_times.append(row.get("time"))
        kept_ids.append(None if row.get("id") is None else str(row["id"]))

    if not kept_obs:
        raise CalibrationError(
            "CALIBRATION_NO_PAIRS",
            f"none of the {len(observed)} observation(s) paired with the model: "
            f"{_why(dropped)}. Nothing here fills that in.")
    return Pairs(observed=tuple(kept_obs), simulated=tuple(kept_sim),
                 times=tuple(kept_times), ids=tuple(kept_ids),
                 dropped=tuple(dropped), units=to_units,
                 quantity=model_quantity or obs_quantity, frame=model_frame,
                 notes=(unit_note, datum_note))


def _why(dropped: Sequence[Mapping[str, Any]]) -> str:
    """The drop reasons with their counts, so a refusal says what went wrong."""
    counts: dict[str, int] = {}
    for row in dropped:
        reason = str(row.get("reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return ", ".join(f"{count} {reason}" for reason, count in sorted(counts.items()))
