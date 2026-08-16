"""Native-safe numeric-formatting helpers shared by the render + chart paths.

Why this module exists (crash post-mortem -- QGIS 4.0.3 / Qt6 / macOS arm64):
a NON-FINITE or degenerate number that reaches a NATIVE Qt double-to-string
call (``QLocale::toString`` / ``QString::number`` and the QGIS color-ramp
legend that sits on top of them) is catastrophic, not cosmetic. Qt derives a
digit count (precision) internally; fed a range whose span is zero (``-log10``
-> ``+inf``) or a NaN/inf bound, that precision is a non-finite double cast to
a C ``int``. On arm64 a non-finite double -> int32 conversion SATURATES to
``INT_MAX`` (``0x7fffffff``) instead of raising, so ``qt_doubleToAscii`` is
asked to emit ~2.1 billion digits and smashes the stack (SIGBUS). Python's own
f-strings would raise on a bad precision -- the danger lives only at the
NATIVE boundary, and only a computed (never a literal) precision reaches it.

The rule these helpers enforce: never hand a computed precision, or an
unvalidated ``(vmin, vmax)`` range, to a native Qt/QGIS formatting API. Clamp
the precision into a sane band; replace a non-finite / degenerate range with a
sane default BEFORE it reaches the renderer.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

#: Decimal places we will ever ask a formatter to emit. 12 is already past
#: double precision's meaningful digits; the point is a hard ceiling far below
#: the INT_MAX that crashes Qt.
MIN_DECIMALS = 0
MAX_DECIMALS = 12
DEFAULT_DECIMALS = 6


def is_finite_number(value) -> bool:
    """True only for a real, finite int/float (NOT bool, NaN or inf)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def clamp_decimals(n, default: int = DEFAULT_DECIMALS) -> int:
    """A decimals/precision value bounded to ``[MIN_DECIMALS, MAX_DECIMALS]``.

    A non-finite / non-numeric ``n`` (the exact input that saturates to
    INT_MAX on arm64) collapses to ``default``. This is the single guard every
    site that feeds a native ``decimals`` / ``precision`` argument routes
    through.
    """
    if not is_finite_number(n):
        return max(MIN_DECIMALS, min(MAX_DECIMALS, int(default)))
    return max(MIN_DECIMALS, min(MAX_DECIMALS, int(n)))


def sane_range(
    vmin, vmax, default: Tuple[float, float] = (0.0, 1.0)
) -> Tuple[float, float]:
    """A finite, strictly-increasing ``(vmin, vmax)`` for a raster/axis range.

    Returns ``default`` when either bound is non-finite (NaN / inf -- the
    values that DEFEAT a plain ``vmax <= vmin`` guard, since every comparison
    with NaN is False) or the span is not positive. Guarantees ``vmin < vmax``
    and both finite, so no degenerate span reaches the native color-ramp
    legend.
    """
    if not (is_finite_number(vmin) and is_finite_number(vmax)):
        return default
    lo, hi = float(vmin), float(vmax)
    if not hi > lo:
        return default
    return lo, hi


def is_sane_range(vmin, vmax) -> bool:
    """Whether ``(vmin, vmax)`` is already finite and strictly increasing --
    i.e. ``sane_range`` would pass it through untouched. Callers use it to
    decide whether they had to substitute a default (an honest style note)."""
    return (
        is_finite_number(vmin)
        and is_finite_number(vmax)
        and float(vmax) > float(vmin)
    )


def decimals_for_range(vmin, vmax, default: int = DEFAULT_DECIMALS) -> int:
    """Bounded decimal places for labelling a value range -- enough to separate
    the endpoints, always inside ``[MIN_DECIMALS, MAX_DECIMALS]``.

    A degenerate / non-finite range yields ``default`` (never a non-finite
    precision). This is the SAFE re-implementation of the ``-log10(span)``
    idiom that, unclamped, is what crashes Qt.
    """
    if not is_sane_range(vmin, vmax):
        return clamp_decimals(default)
    span = float(vmax) - float(vmin)
    if span <= 0.0 or not math.isfinite(span):
        return clamp_decimals(default)
    raw = 1 - math.floor(math.log10(span))
    return clamp_decimals(raw, default=default)


def format_number(
    value, decimals: Optional[int] = None, fallback: str = "n/a"
) -> str:
    """A native-safe number label. Non-finite ``value`` -> ``fallback`` (never
    a "nan"/"inf" string leaking into a legend). ``decimals`` is clamped;
    ``None`` uses Python's compact ``:g``."""
    if not is_finite_number(value):
        return fallback
    v = float(value)
    if decimals is None:
        return f"{v:g}"
    return f"{v:.{clamp_decimals(decimals)}f}"
