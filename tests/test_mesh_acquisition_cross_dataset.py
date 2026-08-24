"""ADR 0223 (audit #7): the TELEMAC rain-on-grid mesh-bed DEM cross-dataset
fallback (3DEP bare-earth -> Copernicus GLO-30 DSM) must be UNCONDITIONALLY
labeled -- a caller that passes no ``notes`` sink cannot silently ingest the
canopy-inclusive surface model as the mesh bed.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from trid3nt_server.workflows.telemac.rain_on_grid import mesh_acquisition as mod


def _patch_3dep_unavailable(monkeypatch, tmp_path: Path) -> Path:
    """fetch_dem (3DEP) raises; fetch_copernicus_dem returns a local temp tif."""
    cop_tif = tmp_path / "copernicus.tif"
    cop_tif.write_bytes(b"\x00cop")

    def _boom_3dep(**kwargs):
        raise RuntimeError("3DEP tile server 404 for this AOI")

    def _fake_copernicus(**kwargs):
        return types.SimpleNamespace(uri=str(cop_tif))

    fake_registry = {
        "fetch_dem": types.SimpleNamespace(fn=staticmethod(_boom_3dep)),
        "fetch_copernicus_dem": types.SimpleNamespace(fn=staticmethod(_fake_copernicus)),
    }
    import trid3nt_server.tools as _tools
    monkeypatch.setattr(_tools, "TOOL_REGISTRY", fake_registry)
    return cop_tif


def test_cross_dataset_fallback_appends_note_when_sink_present(monkeypatch, tmp_path):
    """With a ``notes`` sink, the canopy-inclusive fallback is recorded (loud)."""
    _patch_3dep_unavailable(monkeypatch, tmp_path)
    notes: list[str] = []
    out = mod._resolve_bare_earth_dem(
        tmp_path, (-82.0, 35.5, -81.9, 35.6), None,
        resolution_m=10, filename="bed.tif", notes=notes)
    assert isinstance(out, Path) and out.exists()
    assert any("CROSS-DATASET FALLBACK" in n for n in notes)
    assert any("canopy-inclusive" in n for n in notes)


def test_cross_dataset_fallback_raises_when_no_sink(monkeypatch, tmp_path):
    """ADR 0223: with NO ``notes`` sink the swap RAISES rather than silently
    ingesting the DSM -- the label cannot be bypassed by the call shape."""
    _patch_3dep_unavailable(monkeypatch, tmp_path)
    with pytest.raises(mod.MeshAcquisitionError) as ei:
        mod._resolve_bare_earth_dem(
            tmp_path, (-82.0, 35.5, -81.9, 35.6), None,
            resolution_m=10, filename="bed.tif", notes=None)
    assert ei.value.error_code == "MESH_BED_DEM_CROSS_DATASET_FALLBACK"
    assert "canopy-inclusive" in str(ei.value)
