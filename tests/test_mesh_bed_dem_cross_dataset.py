"""ADR 0223 (audit #7), re-homed onto the shared mesh front's ``Data`` producer
contract: the mesh-bed DEM cross-dataset fallback (3DEP bare-earth -> Copernicus
GLO-30 DSM) must be UNCONDITIONALLY labeled -- the label rides the RETURNED
artifact (``cross_dataset`` + ``note``), which every consumer reads, rather than
an out-parameter a caller could decline to pass. That is what makes it
unbypassable now, in place of the old raise-when-no-sink shape.
"""

from __future__ import annotations

import logging
import types

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.mesh.watershed import resolve_bed_dem

_BBOX = (-82.0, 35.5, -81.9, 35.6)


def test_resolve_bed_dem_pins_3dep(monkeypatch):
    """The mesh BED DEM must be requested from 3DEP bare-earth, never the
    Copernicus DSM (canopy inflates node elevations under tree cover)."""
    seen: dict = {}

    def fake_fetch_dem(**kwargs):
        seen.update(kwargs)
        return types.SimpleNamespace(uri="s3://cache/mesh/bed_3dep.tif")

    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_dem", types.SimpleNamespace(fn=fake_fetch_dem))

    out = resolve_bed_dem(resolution_m=10, bbox=_BBOX)
    assert out["uri"] == "s3://cache/mesh/bed_3dep.tif"
    assert out["source"] == "usgs_3dep_bare_earth"
    assert out["resolution_m"] == 10
    assert out["cross_dataset"] is False
    assert seen["source"] == "3dep"          # bare-earth, not copernicus
    assert seen["resolution_m"] == 10
    assert seen["bbox"] == _BBOX


def test_resolve_bed_dem_loud_copernicus_fallback(monkeypatch, caplog):
    """When 3DEP is unavailable the Copernicus swap must be LOUD: a logged
    warning + the returned artifact's own ``cross_dataset``/``note`` (never a
    silent surface-model substitution)."""

    def fetch_dem_down(**kwargs):
        raise RuntimeError("USGS 3DEP DEM fetch failed (service outage)")

    def fetch_copernicus(**kwargs):
        return types.SimpleNamespace(uri="s3://cache/mesh/bed_copernicus.tif")

    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_dem", types.SimpleNamespace(fn=fetch_dem_down))
    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_copernicus_dem",
        types.SimpleNamespace(fn=fetch_copernicus))

    with caplog.at_level(logging.WARNING):
        out = resolve_bed_dem(resolution_m=10, bbox=_BBOX)
    assert out["uri"] == "s3://cache/mesh/bed_copernicus.tif"
    assert out["source"] == "copernicus_glo30"
    assert out["cross_dataset"] is True
    assert "CROSS-DATASET FALLBACK" in out["note"]
    assert "canopy" in out["note"].lower()
    assert any("Copernicus" in r.message for r in caplog.records)
