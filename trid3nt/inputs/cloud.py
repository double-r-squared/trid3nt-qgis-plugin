"""THE CLOUD over a place: one source's cloud cover as an octa series on the run's clock.

It reads the weather record's one quantity: the nearest source whose record brackets the run, its
cloud_cover member only. A source that reports cloud as sky-cover categories says so on the run."""

from __future__ import annotations

from typing import Any, Mapping

from trid3nt_contracts.coverage import Member
from trid3nt.errors import cloud_cover_from_asos_warning

from .observation import ObservationError
from .run_notes import journal_note
from .weather import _UNSOURCED, Weather, weather

__all__ = ["cloud"]

_CODE = "CLOUD_INVALID"
_KEPT = ("site", "time", "cloud_cover", "cloud_cover_accuracy")


def cloud(source: Any, *, fields: Mapping[str, Member] | None = None, producer: str = "", near: Any = None,
          at: Any = None, window_s: Any = None, label: str = "cloud", code: str = _CODE) -> Weather | None:
    """A fetched record -> the cloud_octas series of the nearest source that brackets the run."""
    if source is None:
        return None
    stated = {name: member for name, member in (fields or _UNSOURCED).items() if name in _KEPT}
    if "cloud_cover" not in stated:
        raise ObservationError(code, f"{label} maps no 'cloud_cover' member, so it reports no cloud: "
                                     "state the cloud cover, or name a source that reports it.")
    found = weather(source, fields=stated, producer=producer, near=near, at=at, window_s=window_s,
                    label=label, code=code)
    if stated["cloud_cover"].table is not None:
        journal_note(cloud_cover_from_asos_warning())
    return found
