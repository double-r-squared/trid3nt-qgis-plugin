"""Reading ABI bands off a layer by their ABI band number.

A raw ABI layer states each band's number, wavelength and unit in that band's
description, so a derive over one asks for the numbers it needs rather than for
positions it cannot check, and the layer's own tags carry what the scan was
observed at. Callers supply their own typed-error factory, so each tool raises its
own error class and code.
"""

from __future__ import annotations

import os
import re
import tempfile
from typing import Any, Callable, Sequence

from trid3nt_server.inputs.layer_fields import layer_field
from trid3nt_server.tools.derive._gdal_runner import read_raster_bytes

__all__ = ["ABI_BAND_RE", "band_positions", "read_bands"]

#: The leading token of an ABI band description. The number is the only part a
#: reader matches on; the wavelength and the quantity after it are for a person.
ABI_BAND_RE = re.compile(r"^\s*ABI band (\d{1,2})\b")


def band_positions(descriptions: Sequence[Any]) -> dict[int, int]:
    """``{abi band number: 1-based band position}`` for every described band."""
    found: dict[int, int] = {}
    for position, description in enumerate(descriptions or (), start=1):
        match = ABI_BAND_RE.match(str(description or ""))
        if match is not None:
            found.setdefault(int(match.group(1)), position)
    return found


def _uri_of(layer: Any, on_error: Callable[[str], Exception]) -> str:
    """The layer's raster uri, whether it arrived as a layer, a mapping or a path."""
    uri = layer if isinstance(layer, str) else layer_field(layer, "uri")
    if not isinstance(uri, str) or not uri.strip():
        raise on_error(
            f"expected a raster layer with a uri; got {layer!r}. Pass the layer a "
            "raw ABI fetch returned, or its uri.")
    return uri.strip()


def read_bands(
    layer: Any,
    bands: Sequence[int],
    *,
    on_error: Callable[[str], Exception],
) -> tuple[dict[int, Any], dict[str, Any]]:
    """Read the named ABI bands as float32 arrays with no-data as NaN, plus the
    layer's grid and its dataset tags. A layer missing any requested band raises
    through ``on_error``, naming the bands it does carry."""
    import numpy as np
    import rasterio

    payload = read_raster_bytes(_uri_of(layer, on_error), on_error=on_error)
    fd, path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_abi_read_")
    os.close(fd)
    try:
        with open(path, "wb") as handle:
            handle.write(payload)
        try:
            src = rasterio.open(path)
        except Exception as exc:  # noqa: BLE001 -- any reader fault, named
            raise on_error(f"the layer is not a readable raster: {exc}") from exc
        with src:
            positions = band_positions(src.descriptions)
            missing = [b for b in bands if b not in positions]
            if missing:
                raise on_error(
                    f"the layer carries no ABI band {missing}; it carries "
                    f"{sorted(positions) or 'no described ABI bands'}. Fetch the raw "
                    "ABI bands this computation needs and hand that layer over.")
            arrays: dict[int, Any] = {}
            for band in bands:
                position = positions[band]
                values = src.read(position).astype(np.float32)
                nodata = src.nodatavals[position - 1]
                if nodata is not None and not np.isnan(nodata):
                    values[values == np.float32(nodata)] = np.nan
                arrays[band] = values
            grid = {
                "transform": src.transform,
                "crs": src.crs,
                "width": src.width,
                "height": src.height,
                "bounds": tuple(src.bounds),
                "tags": dict(src.tags()),
            }
        return arrays, grid
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
