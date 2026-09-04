#!/usr/bin/env python3
"""Live E2E: a declared style row reaches the canvas as a .qml QGIS loads.

Drives REAL fetches through the router and follows the style the whole way: the
spec's own ``style:`` row -> the router's per-call resolution -> the emitted
layer -> the publish path's ONE resolution against the raster's own bytes ->
the ``.qml``. The final assertion is made by QGIS itself: the document is loaded
onto the PRODUCED artifact and the POST-LOAD state is read back, because
``loadNamedStyle``'s boolean reports well-formedness and nothing else.

Two fetches, so both ends of the family are proven live: a DEM (a continuous
raster whose row declares a grey ramp in metres) and a river geometry (a vector
reference layer whose row declares a line symbol).

Two phases, because the agent venv is python 3.12 and QGIS's bindings are built
for the system 3.13 - they cannot share an interpreter:

    set -a; source .env.local; set +a
    venvs/agent/bin/python scripts/proof_declared_style_live.py fetch
    QT_QPA_PLATFORM=offscreen python3 scripts/proof_declared_style_live.py validate
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HANDOFF = Path(tempfile.gettempdir()) / "trid3nt_declared_style_proof.json"

#: A small AOI over the Coweeta basin - a real 3DEP tile and real OSM waterways.
AOI = [-83.46, 35.03, -83.42, 35.07]

_FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not ok:
        _FAILURES.append(f"{name}: {detail}")


# --------------------------------------------------------------------------- #
# phase 1: the real fetch, under the agent venv
# --------------------------------------------------------------------------- #

def _download(uri: str, suffix: str) -> str:
    import os

    import boto3

    bucket, key = uri[len("s3://"):].split("/", 1)
    out = Path(tempfile.mkdtemp()) / f"artifact{suffix}"
    boto3.client(
        "s3",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3")
        or os.environ.get("TRID3NT_S3_ENDPOINT"),
    ).download_file(bucket, key, str(out))
    return str(out)


def _call(fn, **kwargs):
    """A registered fetcher is sync or async depending on its own shape."""
    import asyncio
    import inspect

    out = fn(**kwargs)
    return asyncio.run(out) if inspect.isawaitable(out) else out


def fetch() -> int:
    sys.path.insert(0, str(REPO))

    import trid3nt_server.tools as _bootstrap  # noqa: F401 -- init the registry first
    from trid3nt_server.emission import presets
    from trid3nt_server.emission.publish import legend_for_published_layer
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree

    specs = compose_specs_from_tree(REPO / "trid3nt_server" / "tools" / "fetchers")
    out: dict = {}

    print("continuous raster (fetch_dem):")
    declared = specs["fetch_dem"].output.style
    check("dem / the spec declares its own row",
          declared is not None and declared.get("label") == "Elevation",
          json.dumps(declared))
    layer = _call(TOOL_REGISTRY["fetch_dem"].fn, bbox=AOI)
    check("dem / the fetched layer carries the declared row",
          layer.style == declared, json.dumps(layer.style))
    legend = legend_for_published_layer(layer.style, layer.uri)
    check("dem / the publish path resolves it against the raster's own bytes",
          legend is not None and legend.vmax is not None,
          f"{legend.vmin}..{legend.vmax}" if legend else "None")
    check("dem / the resolved style ships a .qml",
          legend is not None and legend.qml is not None)
    out["raster"] = {"path": _download(layer.uri, ".tif"), "qml": legend.qml,
                     "ramp": list(presets.ramp_stops(legend.colormap)),
                     "vmin": legend.vmin, "vmax": legend.vmax,
                     "units": legend.units}

    print("reference vector (fetch_river_geometry):")
    declared = specs["fetch_river_geometry"].output.style
    check("river / the spec declares a line reference",
          declared == {"kind": "reference", "geometry": "line"}, json.dumps(declared))
    layer = _call(TOOL_REGISTRY["fetch_river_geometry"].fn, bbox=AOI)
    check("river / the fetched layer carries the declared row",
          layer.style == declared, json.dumps(layer.style))
    resolved = presets.resolve(presets.from_row(layer.style))
    check("river / a reference layer is drawn, not measured", resolved.range is None,
          resolved.legend_note())
    out["vector"] = {"path": _download(layer.uri, ".fgb"), "qml": resolved.qml()}

    HANDOFF.write_text(json.dumps(out), encoding="utf-8")
    print(f"\nhandoff -> {HANDOFF}")
    return _verdict()


# --------------------------------------------------------------------------- #
# phase 2: the installed QGIS reads back what was shipped
# --------------------------------------------------------------------------- #

def validate() -> int:
    from qgis.core import QgsApplication

    QgsApplication.setPrefixPath("/usr", True)
    app = QgsApplication([], False)
    app.initQgis()
    try:
        data = json.loads(HANDOFF.read_text(encoding="utf-8"))
        _validate_raster(data["raster"])
        _validate_vector(data["vector"])
    finally:
        app.exitQgis()
    return _verdict()


def _write(qml: str, stem: str) -> str:
    path = Path(tempfile.mkdtemp()) / f"{stem}.qml"
    path.write_text(qml, encoding="utf-8")
    return str(path)


def _validate_raster(row: dict) -> None:
    from qgis.core import QgsRasterLayer

    print("continuous raster (the produced 3DEP COG):")
    layer = QgsRasterLayer(row["path"], "dem", "gdal")
    check("dem / the produced COG opens", layer.isValid())
    before = layer.renderer().type()
    _msg, ok = layer.loadNamedStyle(_write(row["qml"], "dem"))
    check("dem / QGIS loads the shipped document", bool(ok))
    check("dem / QGIS ends up holding a pseudocolor renderer",
          layer.renderer().type() == "singlebandpseudocolor", layer.renderer().type())
    check("dem / the render actually changed", layer.renderer().type() != before, before)
    items = layer.renderer().shader().rasterShaderFunction().colorRampItemList()
    check("dem / the ramp QGIS holds is the declared one",
          [i.color.name() for i in items] == row["ramp"],
          str([i.color.name() for i in items]))
    check("dem / the range QGIS holds is the resolved range",
          (round(items[0].value, 6), round(items[-1].value, 6))
          == (round(row["vmin"], 6), round(row["vmax"], 6)),
          f"{items[0].value}..{items[-1].value} vs {row['vmin']}..{row['vmax']}")
    check("dem / the legend QGIS holds carries the declared units",
          items[-1].label.endswith(f" {row['units']}"), items[-1].label)


def _validate_vector(row: dict) -> None:
    from qgis.core import QgsVectorLayer

    print("reference vector (the produced OSM waterways FlatGeobuf):")
    layer = QgsVectorLayer(row["path"], "river", "ogr")
    check("river / the produced FlatGeobuf opens", layer.isValid())
    _msg, ok = layer.loadNamedStyle(_write(row["qml"], "river"))
    check("river / QGIS loads the shipped document", bool(ok))
    symbol = layer.renderer().symbol()
    check("river / the symbol QGIS holds matches the declared geometry",
          symbol.symbolLayer(0).layerType() == "SimpleLine",
          symbol.symbolLayer(0).layerType())
    # The declaration has to match the DATA, or the symbol loads and draws
    # nothing - which is the failure mode a boolean check cannot see.
    check("river / and the data really is a line layer",
          layer.geometryType() == 1, str(layer.geometryType()))


def _verdict() -> int:
    if _FAILURES:
        print(f"\nFAILED {len(_FAILURES)}:")
        for line in _FAILURES:
            print(f"  {line}")
        return 1
    print("\nall_passed")
    return 0


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "fetch"
    raise SystemExit(fetch() if phase == "fetch" else validate())
