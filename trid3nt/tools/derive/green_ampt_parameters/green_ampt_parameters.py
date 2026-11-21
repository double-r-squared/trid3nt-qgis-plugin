"""``green_ampt_parameters`` - the USDA texture class of each cell and the Green-Ampt parameters of that class.

Reads sand, silt and clay percent rasters on one grid (and organic carbon for silt cells); fetches nothing. The class is
cut on the fractions as given by the USDA Soil Survey Manual's definitions (chapter 3). Every class but silt takes Table 2
of Rawls, Brakensiek and Miller (1983), J. Hydraulic Engineering 109(1): 62-70; silt, which that table has no row for,
takes the Saxton and Rawls (2006) pedotransfer equations, SSSAJ 70: 1569-1578, through the 1983 paper's Eqs. 4 and 6.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

from trid3nt_contracts.execution import LayerURI, layer_seed
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt.tools import register_tool
from trid3nt.tools.derive import DeriveError

__all__ = ["CLASSES", "OM_PER_OC", "PARAMETERS", "SILT_SOURCE", "SOURCE", "classes", "green_ampt_parameters",
           "parameters", "saxton_rawls", "silt_parameters"]

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
SILT_SOURCE = ("silt cells: Saxton and Rawls (2006), Soil Sci. Soc. Am. J. 70: 1569-1578, Table 1 Eqs. 1-5, 15, 16 and 18 at "
               "normal density, with K = Ks/2 (Eq. 6) and the suction by Eq. 4 of Rawls, Brakensiek and Miller (1983); "
               "organic matter = 1.724 x organic carbon (Kellogg Soil Survey Laboratory Methods Manual, SSIR 42 v5.0, 2014)")
#: Organic matter per unit organic carbon: the Van Bemmelen factor the Kellogg Soil Survey Laboratory calculates organic
#: matter with (SSIR 42 v5.0, 2014, p. 464), the laboratory whose database Saxton and Rawls (2006) regressed on.
OM_PER_OC = 1.724
# Saxton and Rawls (2006) dropped samples above 8 %w organic matter and state the equations are not applied beyond it.
_OM_LIMIT = 8.0
# A tension of 1 kPa as a head of water in cm: 1000 Pa / (1000 kg/m3 x 9.80665 m/s2) x 100.
_CM_PER_KPA = 100.0 / 9.80665
_NAMES = list(CLASSES) + ["silt"]
_SILT = len(CLASSES)
_FRACTIONS = ("sand", "silt", "clay")
_STYLE = {"kind": "continuous"}


def classes(sand: Any, silt: Any, clay: Any) -> Any:
    """The index into the class names of each cell (``silt`` last) by the manual's definitions on the fractions as given:
    -1 where no class holds the cell, -2 where two do (fractions not summing to 100 can do either)."""
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
    hits = np.stack([np.asarray(rule) for rule in rules])
    count = hits.sum(axis=0)
    return np.where(count == 1, hits.argmax(axis=0), np.where(count == 0, -1, -2)).astype("int16")


def parameters(found: Any, column: int) -> Any:
    """One parameter of Table 2 over class indices: ``NODATA`` where the class is unclassed or has no row (silt)."""
    import numpy as np

    table = np.array([row[column] for row in CLASSES.values()] + [NODATA], dtype="float32")
    return np.where(found >= 0, table[np.clip(found, 0, len(table) - 1)], np.float32(NODATA)).astype("float32")


def saxton_rawls(sand: Any, clay: Any, organic_matter: Any) -> tuple[Any, Any, Any, Any]:
    """Saxton and Rawls (2006) Table 1 at normal density -> ``(Ks mm/h, saturated moisture, air-entry tension kPa, lambda)``.

    ``sand`` and ``clay`` are decimal fractions by weight and ``organic_matter`` is percent by weight, as the paper states them.
    """
    import numpy as np

    s, c, om = (np.asarray(v, dtype="float64") for v in (sand, clay, organic_matter))
    t1500t = -0.024 * s + 0.487 * c + 0.006 * om + 0.005 * s * om - 0.013 * c * om + 0.068 * s * c + 0.031
    t1500 = t1500t + (0.14 * t1500t - 0.02)
    t33t = -0.251 * s + 0.195 * c + 0.011 * om + 0.006 * s * om - 0.027 * c * om + 0.452 * s * c + 0.299
    t33 = t33t + (1.283 * t33t ** 2 - 0.374 * t33t - 0.015)
    ts33t = 0.278 * s + 0.034 * c + 0.022 * om - 0.018 * s * om - 0.027 * c * om - 0.584 * s * c + 0.078
    ts33 = ts33t + (0.636 * ts33t - 0.107)
    pet = -21.67 * s - 27.93 * c - 81.97 * ts33 + 71.12 * s * ts33 + 8.29 * c * ts33 + 14.05 * s * c + 27.16
    psi_e = pet + (0.02 * pet ** 2 - 0.113 * pet - 0.70)
    theta_s = t33 + ts33 - 0.097 * s + 0.043
    lam = (np.log(t33) - np.log(t1500)) / (np.log(1500.0) - np.log(33.0))
    ks = 1930.0 * (theta_s - t33) ** (3.0 - lam)
    return ks, theta_s, psi_e, lam


def silt_parameters(sand: Any, clay: Any, organic_matter: Any) -> tuple[Any, Any, Any]:
    """Percent sand, clay and organic matter -> ``(K cm/h, wetting-front suction cm, porosity)``, NaN where undefined.

    K = Ks/2 (Rawls et al. 1983 Eq. 6); suction = (2 lambda + 3)/(2 lambda + 2) x psi_b/2 (Eq. 4), psi_b the air-entry tension.
    """
    import numpy as np

    with np.errstate(invalid="ignore", divide="ignore"):
        ks, theta_s, psi_e, lam = saxton_rawls(np.asarray(sand) / 100.0, np.asarray(clay) / 100.0, organic_matter)
        suction = (2.0 * lam + 3.0) / (2.0 * lam + 2.0) * (psi_e * _CM_PER_KPA) / 2.0
        defined = np.isfinite(ks) & np.isfinite(suction) & (ks > 0) & (psi_e > 0) & (lam > 0)
    return tuple(np.where(defined, v, np.nan) for v in (ks / 2.0 / 10.0, suction, theta_s))


def _read(layer: Any, name: str, scratch: str, units: tuple[str, ...] = ("percent", "%")) -> tuple[Any, Any]:
    """One fraction raster as ``(values with NaN where nodata, the rasterio profile)``, or the refusal naming it."""
    import numpy as np
    import rasterio

    from trid3nt.inputs.geometry import source_uri
    from trid3nt.render.uri_registry import lookup_layer_for_handle
    from trid3nt.tools.derive._hydrology_common import _stage_uri_local

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
    if stated is not None and str(stated).strip().lower() not in units:
        raise DeriveError("GREEN_AMPT_UNITS", f"the {name} raster states its units as {stated!r}; this tool reads {units[0]}.")
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
        from trid3nt.store import objects as storage

        bucket, key = storage.runs_bucket(), f"green-ampt-{stem}/{filename}"
        with open(local, "rb") as written:
            storage.client().put_object(Bucket=bucket, Key=key, Body=written.read())
    return f"s3://{bucket}/{key}"


def derived(sand: Any, silt: Any, clay: Any, organic_carbon: Any = None, *,
            _output_dir: str | None = None) -> list[LayerURI]:
    """The derivation itself, over layers already resolved: what the tool runs."""
    import numpy as np

    from trid3nt.inputs.run_notes import journal_note

    asked = dict(zip(_FRACTIONS, (sand, silt, clay)))
    with tempfile.TemporaryDirectory(prefix="green-ampt-") as scratch:
        read = {name: _read(layer, name, scratch) for name, layer in asked.items()}
        if organic_carbon is not None:
            read["organic carbon"] = _read(organic_carbon, "organic carbon", scratch, units=("g/kg",))
    shapes = {name: (p["shape"], p["transform"], p["crs"]) for name, (_, p) in read.items()}
    if len({repr(s) for s in shapes.values()}) != 1:
        raise DeriveError(
            "GREEN_AMPT_GRID_MISMATCH",
            "the " + ", ".join(read) + " rasters are not on one grid (" + "; ".join(
                f"{n}: {s[0]} cells, {s[1]!r}" for n, s in shapes.items()) + "); fetch them over the same box and depth.")
    fractions = {name: read[name][0] for name in _FRACTIONS}
    profile = read["sand"][1]
    stack = np.stack([fractions[f] for f in _FRACTIONS])
    valid = np.isfinite(stack).all(axis=0) & (stack >= 0).all(axis=0)
    if not valid.any():
        raise DeriveError("GREEN_AMPT_NO_TEXTURE", "no cell carries all three of sand, silt and clay; there is nothing to classify.")
    with np.errstate(invalid="ignore"):
        found = np.where(valid, classes(*(fractions[f] for f in _FRACTIONS)), -1)
    values = {name: parameters(found, column) for name, _, column in PARAMETERS}
    tabled = valid & (found >= 0) & (found != _SILT)
    counts = ", ".join(f"{_NAMES[i]} {int((found == i).sum())}" for i in range(len(CLASSES)) if (found == i).any())
    journal_note(f"{int(valid.sum())} of {valid.size} cells carry a texture; {int(tabled.sum())} take their class's row of "
                 f"Rawls, Brakensiek and Miller (1983) Table 2 ({counts or 'none'}).")
    for code, why in ((-1, "fall in no USDA texture class"), (-2, "fall in two USDA texture classes")):
        if (valid & (found == code)).any():
            journal_note(f"{int((valid & (found == code)).sum())} cells {why} on their sand, silt and clay as given "
                         "(the three do not sum to 100) and are left nodata.")
    by_saxton = _silt(found == _SILT, fractions, read.get("organic carbon"), values, journal_note)
    west, south, east, north = (float(v) for v in (profile["transform"] * (0, profile["shape"][0])
                                                   + profile["transform"] * (profile["shape"][1], 0)))
    seed = layer_seed()
    layers = []
    for name, units, _column in PARAMETERS:
        uri = _written(values[name], profile, f"green_ampt_{name}_{seed}", _output_dir)
        layers.append(LayerURI.published(
            f"green-ampt-{name.replace('_', '-')}", seed=seed, name=name.replace("_", " ").capitalize(),
            layer_type="raster", uri=uri, style=_STYLE, role="primary", quantity=f"green_ampt_{name}", units=units,
            bbox=(west, south, east, north), notes=[SOURCE, *([SILT_SOURCE] if by_saxton else [])]))
    return layers


def _silt(cells: Any, fractions: dict[str, Any], carbon: Any, values: dict[str, Any], note: Any) -> int:
    """Write the Saxton and Rawls values into the silt cells of ``values`` -> how many cells took them.

    Effective porosity stays nodata: the equations state no residual water content.
    """
    import numpy as np

    count = int(cells.sum())
    if not count:
        return 0
    if carbon is None:
        note(f"{count} cells are the texture class silt, which Table 2 has no row for; their Saxton and Rawls (2006) values "
             "need the soil's organic matter, and no organic carbon raster was given (fetch_soilgrids soil_property=soc "
             "over the same box and depth), so they are left nodata.")
        return 0
    organic_matter = carbon[0] / 10.0 * OM_PER_OC
    held = cells & np.isfinite(organic_matter) & (organic_matter >= 0) & (organic_matter <= _OM_LIMIT)
    k, suction, porosity = silt_parameters(fractions["sand"], fractions["clay"], np.where(held, organic_matter, 0.0))
    held &= np.isfinite(k)
    for name, field in (("hydraulic_conductivity", k), ("wetting_front_suction", suction), ("porosity", porosity)):
        values[name] = np.where(held, field, values[name]).astype("float32")
    note(f"{int(held.sum())} cells are the texture class silt, which Table 2 has no row for: their K, suction and porosity "
         "are Saxton and Rawls (2006) at normal density, K = Ks/2 and the suction by Rawls et al. (1983) Eqs. 6 and 4, "
         f"organic matter = {OM_PER_OC} x organic carbon; their effective porosity is left nodata (the equations state "
         "no residual water content).")
    if count - int(held.sum()):
        note(f"{count - int(held.sum())} silt cells carry no organic carbon, or organic matter above the {_OM_LIMIT:g} %w "
             "Saxton and Rawls (2006) fitted to, and are left nodata.")
    return int(held.sum())


_METADATA = AtomicToolMetadata(name="green_ampt_parameters", ttl_class="live-no-cache", source_class=None)


@register_tool(_METADATA, read_only_hint=False, open_world_hint=True, destructive_hint=False, idempotent_hint=True)
async def green_ampt_parameters(sand: Any = None, silt: Any = None, clay: Any = None, organic_carbon: Any = None,
                                **_extra_ignored: Any) -> list[LayerURI]:
    """DERIVE Green-Ampt infiltration parameters from soil texture: sand, silt and clay percent in, one raster per parameter out.

    ROUTING: "Green-Ampt parameters for this soil", "saturated hydraulic conductivity and wetting front suction from
    sand silt clay", "classify the soil texture and give me infiltration parameters", "effective porosity of this soil".
    New case layers; the inputs are not changed.

    `sand`, `silt` and `clay` are case layer ids of percent rasters on ONE grid, as fetch_soilgrids returns them
    (soil_property sand, silt, clay at the same bbox and depth). Each cell takes its USDA texture class from the three
    fractions as given, and that class's mean parameters (Rawls, Brakensiek and Miller 1983). Silt-class cells, which
    that table lacks, take Saxton and Rawls (2006) and need `organic_carbon` (soil_property soc, g/kg, same grid).

    Returns four rasters: hydraulic_conductivity (the Green-Ampt K, about half the saturated Ks, cm/h),
    wetting_front_suction (cm), porosity and effective_porosity (cm3/cm3). Cells with no texture, in no class or two, or
    silt with no organic carbon stay nodata (counted on the run's notes); effective porosity of silt cells too.
    """
    return await asyncio.to_thread(derived, sand, silt, clay, organic_carbon)
