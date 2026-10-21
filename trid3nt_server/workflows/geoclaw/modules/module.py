"""The GeoClaw wrapper over the engine-neutral module surface.

Where the dictionary lives and the one body the engine exposes; the fill holds
this dictionary, and Clawpack's own writer judges what the authoring writes
(setrun's Data classes). One module word, "geoclaw": every rundata group is a
keyword prefix on the same body, and what a solved run writes is a later leg's
to pin.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from ...runtime.module import Module
from ..errors import GeoclawError

__all__ = ["GeoclawModule", "module_input_dir", "topography"]

#: What one Topography statement may carry: the surface, and the offset onto the run's frame.
_SURFACE = ("surface", "offset")


def module_input_dir() -> Path:
    """Where the committed module inputs live."""
    return Path(__file__).resolve().parent / "module_input"


def _refuse(code: str, message: str) -> None:
    raise GeoclawError(message, error_code=f"GEOCLAW_{code}")


def topography(value: Any, *, run: Mapping[str, Any]
               ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The bed ladder as topofiles: Topography([{surface, offset}, ...]), coarse to fine.

    Each surface is a bed (a raster or a case layer) read on the run's ``vertical_frame``
    through the bed's own offset path, so a surface on another zero needs its measured
    ``offset`` or refuses. Every surface becomes one topotype-3 lon/lat topofile and one
    TOPO_TOPOFILES row; the engine takes the finest file at each point, so nothing is
    merged or filled."""
    from trid3nt_server.inputs.bed import bed
    from trid3nt_server.inputs.geometry import source_uri

    surfaces = list(value) if isinstance(value, (list, tuple)) else [value]
    if not surfaces:
        _refuse("TOPOGRAPHY_UNSTATED", "Topography states no surface; it takes the bed "
                "surfaces coarse to fine, each a raster or a case layer.")
    if run.get("TOPO_TOPOFILES") is not None:
        _refuse("TOPOGRAPHY_TOPOFILES_STATED",
                "TOPO_TOPOFILES is stated beside Topography: Topography states the "
                "topofile list, so remove the keyword or the composite.")
    frame = str(run.get("vertical_frame") or "").strip()
    if not frame:
        _refuse("TOPOGRAPHY_FRAME_UNSTATED",
                "the run states no vertical_frame, so no surface can be read on the "
                "run's zero: state the frame the run counts elevations from.")
    rows: list[list[Any]] = []
    files: dict[str, Any] = {}
    for number, item in enumerate(surfaces, 1):
        surface, offset = item, None
        if isinstance(item, Mapping):
            extra = sorted(set(item) - set(_SURFACE))
            if extra:
                _refuse("TOPOGRAPHY_UNSTATED",
                        f"Topography surface {number} states {', '.join(extra)}; a "
                        f"surface takes {', '.join(_SURFACE)}.")
            surface, offset = item.get("surface"), item.get("offset")
        if surface is None:
            _refuse("TOPOGRAPHY_UNSTATED",
                    f"Topography surface {number} states no surface to read.")
        read = bed(surface, frame=frame, offset=offset,
                   label=f"topography surface {number}")
        if read.is_depth:
            _refuse("TOPOGRAPHY_DEPTH",
                    f"Topography surface {number} is a depth in metres, which is no "
                    "grid a topofile holds: hand it the bed raster.")
        name = f"topo_{number:02d}.tt3"
        rows.append([3, name])
        files[name] = {"topofile": str(source_uri(read.source)), "frame": frame}
    return {"TOPO_TOPOFILES": rows}, files


class GeoclawModule(Module):
    """The GeoClaw wrapper: its dictionary and the registry of the one exposed body."""

    @classmethod
    def dictionary_dir(cls) -> Path:
        return module_input_dir()

    @classmethod
    def wrapper_for(cls, module: str) -> type:
        from . import wrapper_for

        return wrapper_for(module)

    @classmethod
    def sheet(cls, filled: dict[str, Any], files: dict[str, Any],
              run: Mapping[str, Any]) -> Any:
        """The filled sheet a body's slots make: the generic sheet, held immutable.

        Nothing is armed or written into files here - the authoring that turns
        slots into setrun values and Clawpack's .data files is a later leg's."""
        from ...runtime.sheet import Sheet

        return Sheet(body=cls, filled=MappingProxyType(filled),
                     files=MappingProxyType(files),
                     run=MappingProxyType(run))
