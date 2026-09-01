"""The in-container drivers, mounted into the boxes their libraries live in.

Each file here runs INSIDE one image - ``mesh:latest`` for oceanmesh, ``telemac:
latest`` for telapy and the engine's own steering-file reader - and imports
nothing from this package: the mount is the only thing that connects them. They
live in the product tree rather than in a sandbox because the callers beside them
shell them on every build and every authoring.

``drivers_dir()`` is the path a caller mounts; the container never imports this
module.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["drivers_dir"]


def drivers_dir() -> Path:
    """The directory a caller mounts into its box as ``/drivers``."""
    return Path(__file__).resolve().parent
