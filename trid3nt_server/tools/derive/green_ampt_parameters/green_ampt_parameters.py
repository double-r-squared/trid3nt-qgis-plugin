"""``green_ampt_parameters`` - the USDA texture class of each cell and the Green-Ampt parameters of that class.

Reads sand, silt and clay percent rasters on one grid; fetches nothing. The class is cut by the USDA Soil Survey Manual's
class definitions (chapter 3). The parameters are Table 2 of Rawls, Brakensiek and Miller (1983), "Green and Ampt
infiltration parameters from soils data", J. Hydraulic Engineering 109(1): 62-70, the means over each texture class.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

from trid3nt_contracts.execution import LayerURI, layer_seed
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.derive import DeriveError

__all__ = ["CLASSES", "PARAMETERS", "SOURCE", "classes", "green_ampt_parameters", "parameters"]

NODATA = -9999.0
SOURCE = ("Rawls, Brakensiek and Miller (1983), J. Hydraulic Engineering 109(1): 62-70, Table 2 (texture-class means); "
          "texture classes by the USDA Soil Survey Manual (2017), chapter 3")

#: Table 2, one row per class: total porosity (cm3/cm3), effective porosity (cm3/cm3), wetting-front suction head (cm),
#: Green-Ampt hydraulic conductivity K (cm/h). The table has no row for silt.
CLASSES: dict[str, tuple[float, float, float, float]] = {
    "sand": (0.437, 0.417, 4.95, 11.78),
    "loamy sand": (0.437, 0.401, 6.13, 2.99),
    "sandy loam": (0.453, 0.412, 11.01, 1.09),
    "loam": (0.463, 0.434, 8.89, 0.34),
    "silt loam": (0.501, 0.486, 16.68, 0.65),
    "sandy clay loam": (0.398, 0.330, 21.85, 0.15),
    "clay loam": (0.464, 0.309, 20.88, 0.10),
    "silty clay loam": (0.471, 0.432, 27.30, 0.10),
    "sandy clay": (0.430, 0.321, 23.90, 0.06),
    "silty clay": (0.479, 0.423, 29.22, 0.05),
    "clay": (0.475, 0.385, 31.63, 0.03),
}
#: ``(name, units, column of CLASSES)`` of each raster the tool returns.
PARAMETERS = (
    ("hydraulic_conductivity", "cm/h", 3),
    ("wetting_front_suction", "cm", 2),
    ("porosity", "cm3/cm3", 0),
    ("effective_porosity", "cm3/cm3", 1),
)
_NAMES = list(CLASSES) + ["silt"]
_FRACTIONS = ("sand", "silt", "clay")
_STYLE = {"kind": "continuous"}


def classes(sand: Any, silt: Any, clay: Any) -> Any:
    """The index into the class names of each cell (``silt`` last, -1 where a cell is unclassed), by the manual's definitions."""
    import numpy as np

    sand, silt, clay = (np.asarray(v, dtype="float64") for v in (sand, silt, clay))
    rules = (
        (sand > 85) & (silt + 1.5 * clay < 15),
        (sand >= 70) & (sand <= 90) & (silt + 1.5 * clay >= 15) & (silt + 2 * clay < 30),
        (silt + 2 * clay >= 30) & (((clay >= 7) & (clay < 20) & (sand > 52)) | ((clay < 7) & (silt < 50))),
        (clay >= 7) & (clay < 27) & (silt >= 28) & (silt < 50) & (sand <= 52),
        ((silt >= 50) & (clay >= 12) & (clay < 27)) | ((silt >= 50) & (silt < 80) & (clay < 12)),
        (clay >= 20) & (clay < 35) & (silt < 28) & (sand > 45),
        (clay >= 27) & (clay < 40) & (sand > 20) & (sand <= 45),
        (clay >= 27) & (clay < 40) & (sand <= 20),
        (clay >= 35) & (sand > 45),
        (clay >= 40) & (silt >= 40),
        (clay >= 40) & (sand <= 45) & (silt < 40),
        (silt >= 80) & (clay < 12),
    )
    found = np.full(sand.shape, -1, dtype="int16")
    for index in range(len(rules) - 1, -1, -1):
        found[rules[index]] = index
    return found


def parameters(found: Any, column: int) -> Any:
    """One parameter of Table 2 over class indices: ``NODATA`` where the class is unclassed or has no row (silt)."""
    import numpy as np

    table = np.array([row[column] for row in CLASSES.values()] + [NODATA], dtype="float32")
    return np.where(found >= 0, table[np.clip(found, 0, len(table) - 1)], np.float32(NODATA)).astype("float32")


def _read(layer: Any, name: str, scratch: str) -> tuple[Any, Any]:
    """One fraction raster as ``(values with NaN where nodata, the rasterio profile)``, or the refusal naming it."""
    import numpy as np
    import rasterio

    from trid3nt_server.inputs.geometry import source_uri
    from trid3nt_server.render.uri_registry import lookup_layer_for_handle
    from trid3nt_server.tools.derive._hydrology_common import _stage_uri_local

    if layer is None:
        raise DeriveError(
            "GREEN_AMPT_FRACTION_MISSING",
            f"green_ampt_parameters needs the {name} raster as well as {', '.join(f for f in _FRACTIONS if f != name)}; "
            f"fetch {name} (fetch_soilgrids with soil_property={name}) and name the layer it produced.")
    if isinstance(layer, str):
        found = lookup_layer_for_handle(layer)
        if found is None:
            raise DeriveError("GREEN_AMPT_LAYER_UNKNOWN",
                              f"the {name} {layer!r} names no layer in this case; fetch it first and name the layer it produced.")
        layer = found
    stated = layer.get("units") if isinstance(layer, dict) else getattr(layer, "units", None)
    if stated is not None and str(stated).strip().lower() not in ("percent", "%"):
        raise DeriveError("GREEN_AMPT_UNITS", f"the {name} raster states its units as {stated!r}; this tool reads percent.")
    uri = str(source_uri(layer) or "").strip()
    if not uri:
        raise DeriveError("GREEN_AMPT_LAYER_UNKNOWN", f"the {name} layer names no file to read.")
    try:
        with rasterio.open(_stage_uri_local(uri, scratch, name)) as src:
            values = src.read(1).astype("float64")
            profile = {"crs": src.crs, "transform": src.transform, "shape": src.shape, "nodata": src.nodata}
    except Exception as exc:  # noqa: BLE001 - every reader fault, named by layer
        raise DeriveError("GREEN_AMPT_LAYER_UNREADABLE", f"the {name} raster {uri!r} could not be read ({exc}).") from exc
    nodata = profile["nodata"]
    values[~np.isfinite(values) | (values == nodata if nodata is not None else False)] = np.nan
    return values, profile


def _written(values: Any, profile: dict[str, Any], stem: str, output_dir: str | None) -> str:
    """One parameter raster as a COG -> its uri: a local path under ``output_dir``, else a key in the runs bucket."""
    import numpy as np
    import rasterio

    filename = f"{stem}.tif"
    with tempfile.TemporaryDirectory(prefix="green-ampt-") as scratch:
        local = os.path.join(scratch, filename)
        with rasterio.open(local, "w", driver="COG", height=values.shape[0], width=values.shape[1], count=1,
                           dtype="float32", crs=profile["crs"], transform=profile["transform"], nodata=NODATA) as dst:
            dst.write(np.asarray(values, dtype="float32"), 1)
        if output_dir is not None:
            kept = os.path.join(output_dir, filename)
            os.replace(local, kept)
            return kept
        from trid3nt_server.store import objects as storage

        bucket, key = storage.runs_bucket(), f"green-ampt-{stem}/{filename}"
        with open(local, "rb") as written:
            storage.client().put_object(Bucket=bucket, Key=key, Body=written.read())
    return f"s3://{bucket}/{key}"


def derived(sand: Any, silt: Any, clay: Any, *, _output_dir: str | None = None) -> list[LayerURI]:
    """The derivation itself, over layers already resolved: what the tool runs."""
    import numpy as np

    from trid3nt_server.inputs.run_notes import journal_note

    with tempfile.TemporaryDirectory(prefix="green-ampt-") as scratch:
        read = {name: _read(layer, name, scratch) for name, layer in zip(_FRACTIONS, (sand, silt, clay))}
    shapes = {name: (p["shape"], p["transform"], p["crs"]) for name, (_, p) in read.items()}
    if len({repr(s) for s in shapes.values()}) != 1:
        raise DeriveError(
            "GREEN_AMPT_GRID_MISMATCH",
            "the sand, silt and clay rasters are not on one grid (" + "; ".join(
                f"{n}: {s[0]} cells, {s[1]!r}" for n, s in shapes.items()) + "); fetch the three over the same box and depth.")
    fractions = {name: values for name, (values, _) in read.items()}
    profile = read["sand"][1]
    stack = np.stack([fractions[f] for f in _FRACTIONS])
    valid = np.isfinite(stack).all(axis=0) & (stack >= 0).all(axis=0) & (stack.sum(axis=0) > 0)
    total = np.where(valid, stack.sum(axis=0), 1.0)
    scaled = [np.where(valid, fractions[f] / total * 100.0, 0.0) for f in _FRACTIONS]
    found = np.where(valid, classes(*scaled), -1)
    if not valid.any():
        raise DeriveError("GREEN_AMPT_NO_TEXTURE", "no cell carries all three of sand, silt and clay; there is nothing to classify.")
    silt_cells = int((found == _NAMES.index("silt")).sum())
    unclassed = int((valid & (found < 0)).sum())
    counts = ", ".join(f"{_NAMES[i]} {int((found == i).sum())}" for i in range(len(_NAMES)) if (found == i).any())
    journal_note(f"{int(valid.sum())} of {valid.size} cells carry a texture ({counts}); the fractions are scaled to sum to 100 per cell.")
    if silt_cells:
        journal_note(f"{silt_cells} cells are the texture class silt, which Rawls, Brakensiek and Miller (1983) Table 2 has no "
                     "row for; they are left nodata in every parameter raster.")
    if unclassed:
        journal_note(f"{unclassed} cells fall in no USDA texture class and are left nodata.")
    west, south, east, north = (float(v) for v in (profile["transform"] * (0, profile["shape"][0])
                                                   + profile["transform"] * (profile["shape"][1], 0)))
    seed = layer_seed()
    layers = []
    for name, units, column in PARAMETERS:
        uri = _written(parameters(found, column), profile, f"green_ampt_{name}_{seed}", _output_dir)
        layers.append(LayerURI.published(
            f"green-ampt-{name.replace('_', '-')}", seed=seed, name=name.replace("_", " ").capitalize(),
            layer_type="raster", uri=uri, style=_STYLE, role="primary", quantity=f"green_ampt_{name}", units=units,
            bbox=(west, south, east, north), notes=[SOURCE]))
    return layers


_METADATA = AtomicToolMetadata(name="green_ampt_parameters", ttl_class="live-no-cache", source_class=None)


@register_tool(_METADATA, read_only_hint=False, open_world_hint=True, destructive_hint=False, idempotent_hint=True)
async def green_ampt_parameters(sand: Any = None, silt: Any = None, clay: Any = None, **_extra_ignored: Any) -> list[LayerURI]:
    """DERIVE Green-Ampt infiltration parameters from soil texture: sand, silt and clay percent in, one raster per parameter out.

    ROUTING: "Green-Ampt parameters for this soil", "saturated hydraulic conductivity and wetting front suction from
    sand silt clay", "classify the soil texture and give me infiltration parameters", "effective porosity of this soil".
    New case layers; the inputs are not changed.

    `sand`, `silt` and `clay` are case layer ids of percent rasters on ONE grid, as fetch_soilgrids returns them
    (soil_property sand, silt, clay at the same bbox and depth). Each cell takes its USDA texture class from the
    three fractions (scaled to sum to 100), and that class's mean Green-Ampt parameters.

    Returns four rasters: hydraulic_conductivity (K, cm/h, the Green-Ampt conductivity of the class), wetting_front_suction
    (cm), porosity and effective_porosity (cm3/cm3). A cell with no texture stays nodata, and so does a cell of the class
    silt, which the source table has no row for (the count is on the run's notes). A missing fraction or rasters on different
    grids refuse.
    """
    return await asyncio.to_thread(derived, sand, silt, clay)
