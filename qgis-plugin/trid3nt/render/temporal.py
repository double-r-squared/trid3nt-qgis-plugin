"""Frame-sequence temporal grouping -- PURE PYTHON (no PyQGIS / PyQt imports).

Native QGIS Temporal Controller support: frame-sequence rasters the agent
publishes (e.g. ``Flood_depth_step_1..N``, ``GOES ... F+03h``, per-frame ISO
valid-times) are detected here so ``layers.stamp_temporal`` can stamp each
member with a fixed per-frame temporal range and the built-in Temporal
Controller plays them like the web scrubber.

The token patterns and grouping rules are a port of the web LayerPanel's
``parseFrameToken`` / ``detectSequentialGroups`` via the agent's tested
Python port (``services/agent .../tools/extract_timeseries_at_point.py`` --
keep the three in lockstep; the web side is the user-visible grouping this
module must agree with). One adaptation for the plugin: underscores are
normalized to spaces before matching, because exported GeoTIFF layer names
are ``_safe_filename`` stems (``Flood_depth_step_1``) of the original layer
names (``Flood depth step 1``).

Grouping is equally conservative: a group forms only from >= 2 layer names
sharing a stem with strictly-increasing token values.

Range assignment (``assign_frame_ranges``):

  * When EVERY member label is an ISO valid-time (the satellite-animation
    convention) and the times strictly increase, each frame's range begins
    at its valid-time and ends at the next frame's (the last frame reuses
    the preceding interval, or 1 hour for a lone interval-less pair).
  * Otherwise a synthetic clock: base = today 00:00 UTC, 1 hour per step,
    contiguous ranges -- step N covers [base + (N-1)h, base + Nh).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

__all__ = [
    "FrameGroup",
    "FrameMember",
    "assign_frame_ranges",
    "default_base_time",
    "group_frame_layers",
    "parse_frame_token",
]

_FRAME_PATTERNS = (
    # Forecast lead hour: "F+01h", "f+12h", "F+1 h", "+06h"
    (
        re.compile(r"\bf?\+?\s*(\d{1,3})\s*h\b", re.IGNORECASE),
        lambda m: f"F+{int(m.group(1)):02d}h",
    ),
    # Hour token: "hour 3", "hr 06", "h12"
    (
        re.compile(r"\bh(?:ou)?r?\s*\+?(\d{1,3})\b", re.IGNORECASE),
        lambda m: f"hr {int(m.group(1))}",
    ),
    # Step/frame/index: "step 4", "frame 02", "idx 3", "index 12"
    (
        re.compile(r"\b(?:step|frame|idx|index)\s*\+?(\d{1,4})\b", re.IGNORECASE),
        lambda m: f"step {int(m.group(1))}",
    ),
    (
        re.compile(r"\bt\s*\+\s*(\d{1,4})\b", re.IGNORECASE),
        lambda m: f"t+{int(m.group(1))}",
    ),
    (re.compile(r"#\s*(\d{1,4})\b"), lambda m: f"#{int(m.group(1))}"),
    # Day token: "day 1", "d+3"
    (
        re.compile(r"\bd(?:ay)?\s*\+?(\d{1,3})\b", re.IGNORECASE),
        lambda m: f"day {int(m.group(1))}",
    ),
)

#: ISO-8601 UTC valid-time substring, e.g. "2026-06-22T18:05:00Z". When a
#: frame name carries BOTH a step token and an ISO valid-time, the ISO
#: becomes the per-frame LABEL and is stripped from the grouping stem.
_ISO_TIME_RX = re.compile(r"\b(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2})?Z?\b")

_STEM_EDGE_PUNCT = re.compile(r"^[\s,(\-]+|[\s,(\-]+$")

#: The ISO label format ``parse_frame_token`` emits: "2026-06-22 18:05Z".
_ISO_LABEL_RX = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})Z$")

_ONE_HOUR = timedelta(hours=1)


@dataclass
class FrameMember:
    """One layer of a frame sequence, ordered by its token value."""

    name: str
    value: int
    label: str


@dataclass
class FrameGroup:
    """A detected frame sequence: shared stem + value-ordered members."""

    stem: str
    members: List[FrameMember]


def parse_frame_token(name: str) -> Optional[dict]:
    """Parse a monotonic frame token out of a layer name.

    Returns ``{"value": int, "label": str, "stem": str}`` or ``None`` when
    no lead-time / step / index token is present. Underscores are treated as
    spaces (exported-GeoTIFF stems); otherwise this mirrors the web's
    ``parseFrameToken``.
    """
    if not name:
        return None
    normalized = name.replace("_", " ")
    for rx, label_fn in _FRAME_PATTERNS:
        m = rx.search(normalized)
        if m is None:
            continue
        value = int(m.group(1))
        body = normalized[: m.start()] + normalized[m.end():]
        iso = _ISO_TIME_RX.search(body)
        if iso:
            body = body.replace(iso.group(0), " ")
        stem = _STEM_EDGE_PUNCT.sub("", re.sub(r"\s+", " ", body)).strip().lower()
        frame_label = f"{iso.group(1)} {iso.group(2)}Z" if iso else label_fn(m)
        return {"value": value, "label": frame_label, "stem": stem}
    return None


def group_frame_layers(names: List[str]) -> List[FrameGroup]:
    """Group raster layer NAMES into frame sequences by shared stem.

    Conservative (the web convention): only stems with >= 2 members whose
    token values are strictly increasing after sorting form a sequence.
    Returns groups ordered largest-first (stem as the tiebreak); members
    are ordered by token value. The caller filters to raster layers.
    """
    grouped: dict[str, List[FrameMember]] = {}
    for name in names:
        token = parse_frame_token(str(name or ""))
        if token is None:
            continue
        grouped.setdefault(token["stem"], []).append(
            FrameMember(name=str(name), value=token["value"], label=token["label"])
        )

    groups: List[FrameGroup] = []
    for stem, members in grouped.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: m.value)
        values = [m.value for m in members]
        if all(b > a for a, b in zip(values, values[1:])):
            groups.append(FrameGroup(stem=stem, members=members))
    groups.sort(key=lambda g: (-len(g.members), g.stem))
    return groups


def _label_to_datetime(label: str) -> Optional[datetime]:
    """An ISO valid-time LABEL ("2026-06-22 18:05Z") -> aware UTC datetime."""
    m = _ISO_LABEL_RX.match(label or "")
    if m is None:
        return None
    y, mo, d, h, mi = (int(g) for g in m.groups())
    try:
        return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)
    except ValueError:
        return None


def default_base_time(now: Optional[datetime] = None) -> datetime:
    """The synthetic-clock base: today 00:00 UTC."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def assign_frame_ranges(
    group: FrameGroup, base: Optional[datetime] = None
) -> List[Tuple[str, datetime, datetime]]:
    """Per-frame ``(name, begin, end)`` temporal ranges for one group.

    ISO valid-time labels win when every member carries one and they
    strictly increase (begin = the frame's valid-time, end = the next
    frame's; the last frame reuses the preceding interval). Otherwise the
    synthetic clock: step N covers [base + (N-1)h, base + Nh) -- contiguous
    for consecutive token values, never overlapping for sparse ones.
    """
    members = group.members
    iso_times = [_label_to_datetime(m.label) for m in members]
    if (
        len(members) >= 2
        and all(t is not None for t in iso_times)
        and all(b > a for a, b in zip(iso_times, iso_times[1:]))
    ):
        last_interval = iso_times[-1] - iso_times[-2] if len(iso_times) >= 2 else _ONE_HOUR
        ends = list(iso_times[1:]) + [iso_times[-1] + (last_interval or _ONE_HOUR)]
        return [
            (m.name, begin, end)
            for m, begin, end in zip(members, iso_times, ends)
        ]

    base = base or default_base_time()
    return [
        (
            m.name,
            base + (m.value - 1) * _ONE_HOUR,
            base + m.value * _ONE_HOUR,
        )
        for m in members
    ]
