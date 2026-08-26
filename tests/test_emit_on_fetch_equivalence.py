"""ADR 0244 S2 equivalence: the seam surfaces the SAME context inputs the deleted
per-family ``_surface_*`` helpers used to, for the three representative composers
the work order names (landlab, telemac rain_on_grid, sfincs flood).

No network: this pins the two halves that TOGETHER reproduce the old coverage:

  (A) each composer now declares ``purpose="<word>"`` on the router fetch that
      feeds each formerly-hand-surfaced input (so the seam names it the same way
      the helper did), verified by inspecting the composer source; and
  (B) the seam's ``input_layer_name`` maps that purpose word to the SAME
      ``role="context"`` "Input: <word> (...)" row the helper emitted.

Generic proof that the seam FIRES on a nested composer fetch (given a renderable
LayerURI) lives in ``test_emit_on_fetch_seam.py``; here we pin that the per-family
coverage set is preserved, input by input -- no silently lost layer.
"""

from __future__ import annotations

import pathlib
import re
from types import SimpleNamespace

from trid3nt_server.tools.fetchers._router.emit_on_fetch import (
    input_layer_name,
)

_WORKFLOWS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "trid3nt_server" / "workflows"
)


# family -> list of (fetch-call token that must carry a purpose, purpose word).
# The purpose word == the "<what>" the deleted helper put in "Input: <what>".
_FAMILY_INPUTS: dict[str, dict[str, object]] = {
    "landlab": {
        "file": "landlab/susceptibility/susceptibility.py",
        "fetches": [
            ("fetch_3dep_extra(", "terrain"),
            ("fetch_dem(", "terrain"),
        ],
    },
    "rain_on_grid": {
        # the DEM bed, river and land cover all live in the shared mesh front's
        # CATCHMENT strategy - domain-SHAPE fetches, not a TELEMAC fact.
        "file": "mesh/watershed.py",
        "fetches": [
            ('fetch_river_geometry"].fn(', "river geometry"),
            ('fetch_dem"].fn(', "mesh bed"),
            ('fetch_copernicus_dem"].fn(', "mesh bed"),
            ('fetch_landcover"].fn(', "land cover"),
        ],
    },
    "sfincs": {
        "file": "sfincs/flood/flood.py",
        "fetches": [
            ("topobathy_layer = fetch_topobathy(", "topo-bathymetry"),
            ("dem_layer = fetch_dem(", "terrain"),
            ("landcover_result = fetch_landcover(", "land cover"),
            ("river_layer = _river_fn(", "river geometry"),
        ],
    },
}


def _assert_fetch_carries_purpose(src: str, token: str, word: str) -> None:
    """The call beginning at ``token`` must pass ``purpose="<word>"`` within the
    call's argument span (up to the balancing close-paren, best-effort by scanning
    the next few lines)."""
    idx = src.find(token)
    assert idx != -1, f"fetch call {token!r} not found (composer changed shape)"
    window = src[idx: idx + 400]
    assert f'purpose="{word}"' in window, (
        f"fetch {token!r} must carry purpose={word!r} so the emit-on-fetch seam "
        f"names its surfaced input 'Input: {word} (...)' (found: {window[:200]!r})"
    )


def test_landlab_input_fetches_declare_purpose():
    src = (_WORKFLOWS / _FAMILY_INPUTS["landlab"]["file"]).read_text("utf-8")
    for token, word in _FAMILY_INPUTS["landlab"]["fetches"]:
        _assert_fetch_carries_purpose(src, token, word)


def test_rain_on_grid_input_fetches_declare_purpose():
    fam = _FAMILY_INPUTS["rain_on_grid"]
    src = (_WORKFLOWS / fam["file"]).read_text("utf-8")
    for token, word in fam["fetches"]:
        _assert_fetch_carries_purpose(src, token, word)


def test_sfincs_input_fetches_declare_purpose():
    src = (_WORKFLOWS / _FAMILY_INPUTS["sfincs"]["file"]).read_text("utf-8")
    for token, word in _FAMILY_INPUTS["sfincs"]["fetches"]:
        _assert_fetch_carries_purpose(src, token, word)


def _fake_spec(source_class: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=f"fetch_{source_class}",
        source_class=source_class,
        resolution_declarations=[],
        output=SimpleNamespace(layer_type="raster"),
    )


def test_purpose_words_map_to_input_names_the_helpers_used():
    """``input_layer_name`` turns each family's purpose word into the same
    'Input: <word> (...)' provenance the deleted helper emitted -- so the seam's
    surfaced row is name-equivalent, role forced to context by the seam."""
    words = {
        "terrain", "mesh bed", "river geometry", "land cover", "topo-bathymetry",
    }
    for word in words:
        name = input_layer_name(_fake_spec("usgs_3dep"), {}, word)
        assert name.startswith(f"Input: {word} ("), name
        # the composer word wins over the resolved variable/product param.
        name2 = input_layer_name(_fake_spec("nlcd"), {"variable": "dem"}, word)
        assert name2.startswith(f"Input: {word} ("), name2


def test_deleted_surface_helpers_are_gone():
    """Every per-family input-surfacing helper is gone; the seam is the only path.

    The bed-bathymetry one was the last holdout, and its exemption was real
    while it rode a COG the worker sampled inside the container - route() never
    saw that fetch, so no seam could surface it. The reach bed is a declared
    router fetch now, so the exemption expired with the thing it excused.
    """
    gone = [
        "_surface_landlab_dem_input",
        "_surface_watershed_mesh_inputs",
        "_surface_landcover_input",
        "_surface_river_geometry_input",
        "_surface_bed_bathymetry_input",
    ]
    joined = "\n".join(
        p.read_text("utf-8")
        for p in _WORKFLOWS.rglob("*.py")
        if "__pycache__" not in p.parts
    )
    for name in gone:
        assert f"def {name}" not in joined, f"{name} should be deleted (seam covers it)"
