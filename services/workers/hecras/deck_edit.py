"""HEC-RAS unsteady-flow deck reparameterization (engine-landing wave).

TEMPLATE-FIRST reparameterization (ADR 0100 / 0109): the shipped Muncie project's
GEOMETRY is frozen (RASMapper's 2D subgrid tables cannot be rebuilt headless), so
the ONE thing a run varies is the unsteady FLOW forcing -- the inflow hydrograph
in the boundary-condition file (``.bNN``).

Empirically established (2026-08-04, in-container, both scale=1.0 and scale=1.3):
the Linux ``RasUnsteady`` reads the inflow hydrograph from the ``.bNN`` ASCII
boundary file, NOT from the plan HDF's ``Event Conditions`` group -- scaling the
HDF hydrograph left the max water surface bit-identical, while scaling the ``.bNN``
moved it (wet cells 4896 -> 5012, depth_max 20.24 -> 20.62 ft at 1.3x). So THIS is
the authoritative deck edit.

The hydrograph block in a HEC-RAS ``.bNN`` is::

    Upstream Flow Hydrograph - River: White  Reach: Muncie  RS: 15696.24
          25
           0   13500       1   14000       2   14500       3   15000       4   15500
           ...

i.e. a header line, a right-justified count, then ``count`` ``(time, flow)`` pairs
in 8-character right-justified fixed fields, 5 pairs (10 fields) per 80-char line.
``scale_flow_hydrograph`` multiplies every FLOW ordinate (the 2nd of each pair) by
the factor, preserving the exact fixed-field layout, and leaves every other byte
of the deck untouched.

Pure text I/O -- no h5py, no engine, no object store. Runs from the worker dir
(flat-import lesson) AND is unit-testable offline. ASCII only.
"""

from __future__ import annotations

#: Fixed field width HEC-RAS writes hydrograph ordinates in (right-justified).
_FIELD_W = 8
#: Pairs per line (10 fields of 8 chars == 80-char lines).
_PAIRS_PER_LINE = 5

#: Hydrograph block headers we scale (the inflow forcing families). A downstream
#: Normal Depth / Rating Curve boundary is NOT a flow ordinate series and is left
#: untouched.
_FLOW_HEADERS = ("Flow Hydrograph", "Lateral Inflow Hydrograph", "Uniform Lateral Inflow")


class DeckEditError(RuntimeError):
    """A hydrograph block could not be parsed / rewritten."""


def _is_flow_header(line: str) -> bool:
    s = line.strip()
    for h in _FLOW_HEADERS:
        # "Upstream Flow Hydrograph - River: ..." and bare "Flow Hydrograph=" both match.
        if h in s and ("Hydrograph" in s or "Inflow" in s):
            return True
    return False


def _fields(line: str) -> list[str]:
    """Slice a fixed-field data line into its non-blank 8-char fields."""
    body = line.rstrip("\n")
    out = []
    for k in range(0, len(body), _FIELD_W):
        f = body[k : k + _FIELD_W]
        if f.strip():
            out.append(f)
    return out


def scale_flow_hydrograph(text: str, scale: float) -> tuple[str, float, float]:
    """Scale every inflow-hydrograph FLOW ordinate in a ``.bNN`` deck by ``scale``.

    Args:
        text: the full ``.bNN`` boundary-file text.
        scale: the multiplier applied to each flow ordinate (the carrier forcing).

    Returns:
        ``(new_text, base_peak, scaled_peak)`` -- the rewritten deck plus the
        baseline and scaled PEAK flow (across all scaled hydrograph blocks), so the
        caller can report the physical forcing (invariant 1).

    Raises:
        DeckEditError: a hydrograph block's count/fields could not be parsed.
    """
    if not (scale > 0.0) or scale != scale:  # non-positive or NaN
        raise DeckEditError(f"scale must be a positive finite number, got {scale!r}")

    lines = text.splitlines()
    out: list[str] = []
    base_peak = 0.0
    scaled_peak = 0.0
    i = 0
    n_lines = len(lines)
    while i < n_lines:
        line = lines[i]
        out.append(line)
        if not _is_flow_header(line):
            i += 1
            continue

        # Next line: the ordinate count (right-justified integer).
        i += 1
        if i >= n_lines:
            raise DeckEditError("hydrograph header with no count line")
        count_line = lines[i]
        out.append(count_line)
        try:
            n = int(count_line.strip())
        except ValueError as exc:
            raise DeckEditError(
                f"hydrograph count line not an integer: {count_line!r}"
            ) from exc
        if n <= 0:
            i += 1
            continue

        # Read ceil(n/PAIRS_PER_LINE) data lines of (time, flow) pairs.
        pairs: list[tuple[float, float]] = []
        ndata = (n + _PAIRS_PER_LINE - 1) // _PAIRS_PER_LINE
        for _ in range(ndata):
            i += 1
            if i >= n_lines:
                raise DeckEditError("hydrograph block truncated before all ordinates read")
            flds = _fields(lines[i])
            for k in range(0, len(flds) - 1, 2):
                try:
                    t = float(flds[k])
                    q = float(flds[k + 1])
                except ValueError as exc:
                    raise DeckEditError(
                        f"non-numeric hydrograph ordinate: {flds[k:k + 2]!r}"
                    ) from exc
                pairs.append((t, q))
        pairs = pairs[:n]
        if not pairs:
            i += 1
            continue

        base_peak = max(base_peak, max(q for _, q in pairs))
        scaled = [(t, q * scale) for t, q in pairs]
        scaled_peak = max(scaled_peak, max(q for _, q in scaled))

        # Rewrite the ordinates: 8-char right-justified integer fields, 5 pairs/line.
        buf: list[str] = []
        for idx, (t, q) in enumerate(scaled):
            buf.append(f"{int(round(t)):{_FIELD_W}d}{int(round(q)):{_FIELD_W}d}")
            if (idx + 1) % _PAIRS_PER_LINE == 0:
                out.append("".join(buf))
                buf = []
        if buf:
            out.append("".join(buf))
        i += 1

    new_text = "\n".join(out)
    if text.endswith("\n"):
        new_text += "\n"
    return new_text, base_peak, scaled_peak
