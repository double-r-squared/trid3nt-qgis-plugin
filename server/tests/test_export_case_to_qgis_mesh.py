"""Tests for the ``export_case_to_qgis`` mesh (MDAL phase 1) additive field.

Every SFINCS flood-depth layer (``style_preset == "continuous_flood_depth"``)
whose ``uri`` lives under a runs-bucket ``s3://<bucket>/<run_id>/...`` prefix
is checked for a sibling ``<run_id>/sfincs_map.nc``; when found, ONE entry per
distinct ``run_id`` is appended to the result's ``mesh`` list. No network / no
real S3: a fake boto3-shaped client is monkeypatched onto
``export_case_to_qgis._s3_client`` (the export tool's own local S3 seam).

Coverage:
1. a runs-bucket flood-depth layer with a mesh sibling -> one mesh entry,
   ``crs_authid`` resolved from a synthetic ``sfincs_map.nc``.
2. no mesh sibling (HeadObject miss) -> no entry.
3. a non-flood case (no ``continuous_flood_depth`` layer) -> mesh stays [],
   everything else about the export unchanged.
4. peak + per-frame layers sharing one ``run_id`` -> ONE mesh entry, not one
   per layer (dedup).
5. mesh sibling exists but the NetCDF is unreadable -> entry still lists,
   ``crs_authid`` is ``None`` (honest degrade, never drops the mesh).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("xarray")
pytest.importorskip("pyproj")

from trid3nt_server.tools.meta import export_case_to_qgis as export_mod
from trid3nt_server.tools.meta.export_case_to_qgis import export_case_to_qgis

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Fake S3 client (module-local seam: export_case_to_qgis._s3_client)
# --------------------------------------------------------------------------- #


class _FakeS3Client:
    """HeadObject / GetObject / download_file over an in-memory key set."""

    def __init__(self, existing: set[tuple[str, str]], nc_source: Path | None = None):
        self._existing = existing
        self._nc_source = nc_source
        self.head_calls: list[tuple[str, str]] = []
        self.download_calls: list[tuple[str, str, str]] = []

    def head_object(self, Bucket: str, Key: str) -> dict:
        self.head_calls.append((Bucket, Key))
        if (Bucket, Key) not in self._existing:
            raise Exception(f"404: s3://{Bucket}/{Key} not found")  # noqa: TRY002
        return {}

    def download_file(self, Bucket: str, Key: str, Filename: str) -> None:
        self.download_calls.append((Bucket, Key, Filename))
        if self._nc_source is None or (Bucket, Key) not in self._existing:
            raise Exception(f"404: s3://{Bucket}/{Key} not found")  # noqa: TRY002
        import shutil

        shutil.copyfile(self._nc_source, Filename)

    def get_object(self, Bucket: str, Key: str) -> dict:  # pragma: no cover -- unused here
        raise NotImplementedError


def _install_fake_s3(monkeypatch, existing: set[tuple[str, str]], nc_source: Path | None = None) -> _FakeS3Client:
    client = _FakeS3Client(existing, nc_source)
    monkeypatch.setattr(export_mod, "_s3_client", lambda: client)
    return client


# --------------------------------------------------------------------------- #
# Synthetic sfincs_map.nc (mirrors test_postprocess_flood._make_sfincs_nc)
# --------------------------------------------------------------------------- #


def _make_mesh_nc(tmp_path: Path, *, epsg: str = "EPSG:32616") -> Path:
    import numpy as np
    import xarray as xr

    ds = xr.Dataset(
        {
            "hmax": xr.DataArray(
                np.zeros((1, 2, 2)), dims=["timemax", "n", "m"], attrs={"units": "m"}
            ),
            "crs": xr.DataArray(
                0,
                attrs={"epsg_code": epsg, "grid_mapping_name": "transverse_mercator"},
            ),
        },
        coords={
            "x": xr.DataArray(np.array([0.0, 1.0]), dims=["m"]),
            "y": xr.DataArray(np.array([0.0, 1.0]), dims=["n"]),
        },
    )
    out = tmp_path / "synthetic_sfincs_map.nc"
    ds.to_netcdf(str(out))
    return out


def _flood_layer(name: str, run_id: str, filename: str = "flood_depth_peak.tif") -> dict[str, Any]:
    return {
        "name": name,
        "layer_type": "raster",
        "uri": f"s3://trid3nt-runs/{run_id}/{filename}",
        "style_preset": "continuous_flood_depth",
    }


def _plume_layer(name: str, run_id: str, filename: str = "plume_concentration_4326.tif") -> dict[str, Any]:
    """A MODFLOW plume layer (MDAL phase 2) -- same shape as ``_flood_layer``
    but the ``style_preset`` that maps to ``modflow_mesh.nc`` in
    ``_MESH_SIBLING_BY_STYLE_PRESET``."""
    return {
        "name": name,
        "layer_type": "raster",
        "uri": f"s3://trid3nt-runs/{run_id}/{filename}",
        "style_preset": "continuous_plume_concentration",
    }


def _titiler_flood_layer(name: str, run_id: str, filename: str = "flood_depth_peak.tif") -> dict[str, Any]:
    """A flood-depth layer shaped EXACTLY like a real persisted case layer
    (data/persistence/trid3nt_dev/projects.json): ``uri`` is the TiTiler
    ``/cog/tiles/`` DISPLAY template with the actual ``s3://`` object
    percent-encoded in its ``url=`` query param, not a raw ``s3://`` uri."""
    from urllib.parse import quote

    s3_uri = f"s3://trid3nt-runs/{run_id}/{filename}"
    template = (
        "http://127.0.0.1:8080/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
        f"?url={quote(s3_uri, safe='')}&rescale=0,3&colormap_name=ylgnbu"
    )
    return {
        "name": name,
        "layer_type": "raster",
        "uri": template,
        "style_preset": "continuous_flood_depth",
    }


def _tiny_geotiff_bytes(tmp_path: Path) -> bytes:
    """A minimal valid GeoTIFF's raw bytes -- stands in for the flood-depth
    COG's content so the per-layer raster EXPORT (unrelated to mesh
    discovery) succeeds without any real network I/O."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    path = tmp_path / "_tiny_source.tif"
    data = np.zeros((2, 2), dtype="float32")
    transform = from_bounds(-85.5, 29.9, -85.4, 30.0, 2, 2)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=2,
        width=2,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    ) as ds:
        ds.write(data, 1)
    return path.read_bytes()


def _patch_raster_read(monkeypatch, tmp_path: Path) -> None:
    """Short-circuit ``_read_uri_bytes`` (the tool's OWN inline boto3 read,
    a separate seam from ``_s3_client``/mesh discovery) so an ``s3://``
    flood-depth layer's raster content export never touches real S3."""
    data = _tiny_geotiff_bytes(tmp_path)
    monkeypatch.setattr(export_mod, "_read_uri_bytes", lambda uri: data)


# --------------------------------------------------------------------------- #
# Happy path: mesh sibling exists -> one entry with resolved CRS
# --------------------------------------------------------------------------- #


async def test_mesh_entry_added_with_resolved_crs(tmp_path: Path, monkeypatch) -> None:
    run_id = "01RUNABCDEFGH"
    nc_path = _make_mesh_nc(tmp_path)
    _patch_raster_read(monkeypatch, tmp_path)
    _install_fake_s3(
        monkeypatch,
        existing={("trid3nt-runs", f"{run_id}/sfincs_map.nc")},
        nc_source=nc_path,
    )

    result = await export_case_to_qgis(
        layers=[_flood_layer("Peak flood depth", run_id)],
        output_dir=str(tmp_path / "export"),
    )

    assert result["status"] == "ok"
    assert len(result["mesh"]) == 1
    mesh = result["mesh"][0]
    assert mesh["kind"] == "mesh"
    assert mesh["format"] == "sfincs_map_netcdf"
    assert mesh["s3_uri"] == f"s3://trid3nt-runs/{run_id}/sfincs_map.nc"
    assert mesh["crs_authid"] == "EPSG:32616"
    assert mesh["name"] == f"SFINCS mesh ({run_id[:8]})"
    # Existing fields untouched -- additive only.
    assert result["exported_raster_count"] == 1
    assert result["skipped"] == []


# --------------------------------------------------------------------------- #
# No mesh sibling -> no entry
# --------------------------------------------------------------------------- #


async def test_no_mesh_sibling_yields_no_entry(tmp_path: Path, monkeypatch) -> None:
    run_id = "01RUNNOPEER00"
    _patch_raster_read(monkeypatch, tmp_path)
    fake = _install_fake_s3(monkeypatch, existing=set())  # HeadObject always misses

    result = await export_case_to_qgis(
        layers=[_flood_layer("Peak flood depth", run_id)],
        output_dir=str(tmp_path / "export"),
    )

    assert result["mesh"] == []
    assert fake.head_calls == [("trid3nt-runs", f"{run_id}/sfincs_map.nc")]
    # Mesh discovery is fully independent of per-layer export success -- the
    # raster itself still exports fine (patched read, no live network).
    assert result["status"] == "ok"
    assert result["exported_raster_count"] == 1


# --------------------------------------------------------------------------- #
# Non-flood case -> mesh stays empty, nothing else perturbed
# --------------------------------------------------------------------------- #


async def test_non_flood_case_mesh_stays_empty(tmp_path: Path, monkeypatch) -> None:
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    fake = _install_fake_s3(monkeypatch, existing={("trid3nt-runs", "some-other-run/sfincs_map.nc")})

    raster_path = tmp_path / "dem.tif"
    data = np.linspace(0.0, 3.0, 100, dtype="float32").reshape(10, 10)
    transform = from_bounds(-85.5, 29.9, -85.4, 30.0, 10, 10)
    with rasterio.open(
        raster_path,
        "w",
        driver="GTiff",
        height=10,
        width=10,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    ) as ds:
        ds.write(data, 1)

    result = await export_case_to_qgis(
        layers=[
            {
                "name": "Digital Elevation Model",
                "layer_type": "raster",
                "uri": str(raster_path),
                "style_preset": "elevation_terrain",
            }
        ],
        output_dir=str(tmp_path / "export"),
    )

    assert result["status"] == "ok"
    assert result["mesh"] == []
    # No style_preset=continuous_flood_depth layer -> mesh discovery never
    # even probes S3.
    assert fake.head_calls == []


# --------------------------------------------------------------------------- #
# Peak + per-frame layers sharing a run_id -> dedup to one mesh entry
# --------------------------------------------------------------------------- #


async def test_peak_and_frame_layers_dedup_to_one_mesh_entry(tmp_path: Path, monkeypatch) -> None:
    run_id = "01RUNDEDUP0001"
    nc_path = _make_mesh_nc(tmp_path)
    _patch_raster_read(monkeypatch, tmp_path)
    fake = _install_fake_s3(
        monkeypatch,
        existing={("trid3nt-runs", f"{run_id}/sfincs_map.nc")},
        nc_source=nc_path,
    )

    result = await export_case_to_qgis(
        layers=[
            _flood_layer("Peak flood depth", run_id, filename="flood_depth_peak.tif"),
            _flood_layer("Flood depth step 1", run_id, filename="flood_depth_frame_01.tif"),
            _flood_layer("Flood depth step 2", run_id, filename="flood_depth_frame_02.tif"),
        ],
        output_dir=str(tmp_path / "export"),
    )

    assert len(result["mesh"]) == 1
    assert result["mesh"][0]["s3_uri"] == f"s3://trid3nt-runs/{run_id}/sfincs_map.nc"
    # HeadObject probed once -- the dedup short-circuits before the 2nd/3rd
    # layer would otherwise re-probe the same key.
    assert fake.head_calls == [("trid3nt-runs", f"{run_id}/sfincs_map.nc")]


# --------------------------------------------------------------------------- #
# Mesh sibling exists but its NetCDF is unreadable -> crs_authid=None
# --------------------------------------------------------------------------- #


async def test_titiler_tile_template_uri_resolves_to_mesh_entry(tmp_path: Path, monkeypatch) -> None:
    """Regression: real persisted case layers carry a TiTiler ``/cog/tiles/``
    DISPLAY template as ``uri`` (the actual ``s3://`` object is percent-
    encoded in its ``url=`` param), not a raw ``s3://`` uri -- mesh discovery
    must unwrap it the SAME way the raster export path already does
    (``_unwrap_tile_template``), or every real-world flood-depth layer would
    silently never surface a mesh entry."""
    run_id = "01KWRSKE771W6XVDJRSQDXZYSY"
    nc_path = _make_mesh_nc(tmp_path)
    _patch_raster_read(monkeypatch, tmp_path)
    fake = _install_fake_s3(
        monkeypatch,
        existing={("trid3nt-runs", f"{run_id}/sfincs_map.nc")},
        nc_source=nc_path,
    )

    result = await export_case_to_qgis(
        layers=[_titiler_flood_layer("Peak flood depth", run_id)],
        output_dir=str(tmp_path / "export"),
    )

    assert len(result["mesh"]) == 1
    assert result["mesh"][0]["s3_uri"] == f"s3://trid3nt-runs/{run_id}/sfincs_map.nc"
    assert result["mesh"][0]["crs_authid"] == "EPSG:32616"
    assert fake.head_calls == [("trid3nt-runs", f"{run_id}/sfincs_map.nc")]


async def test_unreadable_mesh_netcdf_lists_entry_with_null_crs(tmp_path: Path, monkeypatch) -> None:
    run_id = "01RUNBADNC0001"
    # HeadObject succeeds, but download_file's "source" is not a valid
    # NetCDF (garbage bytes) -- xr.open_dataset must raise, caught by
    # _resolve_mesh_crs -> None, never a hard failure for the whole export.
    garbage = tmp_path / "not_a_netcdf.bin"
    garbage.write_bytes(b"not a netcdf file")
    _patch_raster_read(monkeypatch, tmp_path)
    _install_fake_s3(
        monkeypatch,
        existing={("trid3nt-runs", f"{run_id}/sfincs_map.nc")},
        nc_source=garbage,
    )

    result = await export_case_to_qgis(
        layers=[_flood_layer("Peak flood depth", run_id)],
        output_dir=str(tmp_path / "export"),
    )

    assert len(result["mesh"]) == 1
    assert result["mesh"][0]["crs_authid"] is None
    assert result["mesh"][0]["s3_uri"] == f"s3://trid3nt-runs/{run_id}/sfincs_map.nc"


# --------------------------------------------------------------------------- #
# MDAL phase 2 (MODFLOW): style_preset="continuous_plume_concentration" ->
# a sibling modflow_mesh.nc, discovered through the SAME _mesh_entry_for_layer
# seam (generalized to _MESH_SIBLING_BY_STYLE_PRESET) -- not a parallel code
# path, so this is a thin slice proving the map dispatch + format id, not a
# re-test of dedup/CRS/TiTiler-unwrap (already covered generically above).
# --------------------------------------------------------------------------- #


async def test_modflow_mesh_entry_added_with_resolved_crs(tmp_path: Path, monkeypatch) -> None:
    run_id = "01RUNMODFLOW0001"
    nc_path = _make_mesh_nc(tmp_path, epsg="EPSG:32617")
    _patch_raster_read(monkeypatch, tmp_path)
    _install_fake_s3(
        monkeypatch,
        existing={("trid3nt-runs", f"{run_id}/modflow_mesh.nc")},
        nc_source=nc_path,
    )

    result = await export_case_to_qgis(
        layers=[_plume_layer("Contaminant Plume (peak concentration)", run_id)],
        output_dir=str(tmp_path / "export"),
    )

    assert len(result["mesh"]) == 1
    mesh = result["mesh"][0]
    assert mesh["format"] == "modflow_ugrid_netcdf"
    assert mesh["s3_uri"] == f"s3://trid3nt-runs/{run_id}/modflow_mesh.nc"
    assert mesh["crs_authid"] == "EPSG:32617"
    assert mesh["name"] == f"MODFLOW mesh ({run_id[:8]})"


async def test_modflow_and_flood_style_presets_probe_distinct_filenames(tmp_path: Path, monkeypatch) -> None:
    """A style_preset with NO map entry (e.g. a plain elevation raster) never
    probes S3 at all; a MODFLOW plume layer probes ONLY modflow_mesh.nc, never
    sfincs_map.nc -- the two engines' sibling filenames must not cross-match."""
    run_id = "01RUNNOCROSS0001"
    fake = _install_fake_s3(monkeypatch, existing=set())  # every HeadObject misses
    _patch_raster_read(monkeypatch, tmp_path)

    result = await export_case_to_qgis(
        layers=[_plume_layer("Contaminant Plume (peak concentration)", run_id)],
        output_dir=str(tmp_path / "export"),
    )

    assert result["mesh"] == []
    assert fake.head_calls == [("trid3nt-runs", f"{run_id}/modflow_mesh.nc")]
