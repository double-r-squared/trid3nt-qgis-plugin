"""MODFLOW UGRID CF-mesh NetCDF emitter (MDAL phase 2, additive).

Extends the SFINCS ``sfincs_map.nc`` mesh seam (``export_case_to_qgis`` /
``postprocess_flood``, MDAL phase 1) to the groundwater engine. Unlike SFINCS
-- whose native cht_sfincs quadtree solve already writes a UGRID-conformant
NetCDF the QGIS MDAL provider opens as-is -- MF6 writes NO mesh file at all
(``gwf_model.hds`` / ``gwt_model.ucn`` are flat binary HEADFILE arrays with no
geometry or CRS). This module BUILDS the mesh: a CF-1.6 / UGRID-1.0 2D
unstructured mesh whose faces are the DIS grid's regular quad cells (node
coordinates + face-node connectivity derived from the deck's georegistration
-- ``xorigin``/``yorigin``/``delr``/``delc``/``nrow``/``ncol``, the SAME fields
``postprocess_modflow._grid_georegistration_from_deck`` already reads for the
COG reprojection transform) carrying TIME-VARYING datasets: ``head`` (every
saved GWF stress-period/timestep) and, when the deck ran GWT transport,
``concentration`` (every saved UCN step). Each is written as ONE CF-UGRID data
variable, so MDAL exposes ONE dataset group with N timesteps per quantity --
better than SFINCS's group-per-time-value quadtree output (a QGIS user gets a
native Temporal Controller-scrubbable series, not N static groups).

**One shared "time" dimension (empirically-required, not a CF nicety).**
Verified against the installed ``libprovider_mdal.so`` (QGIS 3.40 / MDAL): its
netCDF/UGRID reader only recognizes a data variable's extra dimension as time
when that dimension is LITERALLY named ``time`` -- a correctly CF-tagged
(``standard_name="time"``) but differently-NAMED dimension (e.g. the more
"correct"-looking ``time_head``/``time_conc`` pair this module started with)
is silently invisible: MDAL reports ``datasetGroupCount() == 0`` even though
the mesh topology itself loads and validates fine. A netCDF dimension name is
unique per file, so ``head`` and ``concentration`` -- which the REAL spill
deck saves on DIFFERENT schedules (``gwt_adapter``: the GWF head OC is
typically ``saverecord=[("HEAD","LAST")]``, ONE step, while the GWT
concentration OC saves many) -- share ONE ``time`` variable: the SORTED UNION
of both quantities' saved ``totim`` values. A quantity with no saved step at a
shared time slot is NaN there (honest gap, never interpolated/faked -- the
SAME masking convention every other MODFLOW postprocess reader already uses
for dry/inactive cells).

**CRS.** MF6 binary output carries no CRS; MODFLOW's ``model_crs`` handoff
(``DeckStaging.model_crs`` / the OQ-MOD-3 manifest field, e.g. ``"EPSG:32617"``)
is authoritative and known at WRITE time -- no CRS inference is needed. It is
ALSO written into a scalar ``crs`` data variable using the SAME encoding
SFINCS's writer uses (``crs.attrs["epsg_code"]``), so
``export_case_to_qgis._resolve_mesh_crs`` (which calls
``postprocess_flood._read_crs_from_dataset`` -- SFINCS's parser) resolves it
without any MODFLOW-specific CRS-reading code; the ``export_case_to_qgis``
mesh entry's ``crs_authid`` is populated by that SAME shared reader for both
engines.

**Mesh geometry.** The DIS grid is regular (uniform ``delr``/``delc`` --
matches the existing simplification ``_grid_georegistration_from_deck`` already
makes, ``float(mg.delr[0])``/``float(mg.delc[0])``), so cell corners are the
(nrow+1) x (ncol+1) node lattice; each cell's 4 corner nodes form one CCW quad
face (bottom-left -> bottom-right -> top-right -> top-left, standard
math-orientation winding for an x-east/y-north plane). Faces are ordered
row-major (``face_id = row*ncol + col``) so a saved (nrow, ncol) head/
concentration grid flattens onto the face dimension with a plain ``.reshape(-1)``
-- no reordering. Multi-layer decks are reduced to the max-over-layers 2D grid
per saved step (mirrors every other MODFLOW postprocess reduction --
``_read_head_steps`` / ``_read_final_concentration`` / the drawdown/mounding/
hydroperiod readers all do the same collapse; a full 3D layer-resolved mesh is
future work, not needed for the flagship spill/plume path).

**Wiring (this phase).** ``emit_modflow_mesh_artifact`` is called ONLY from
``postprocess_modflow.postprocess_modflow`` (the flagship spill/plume path,
``run_modflow_job``) -- best-effort / non-fatal, exactly like
``_dispatch_publish_layer``: a build or upload failure is logged and returns
``None``, never sinks the plume COG result. The GWF-only archetype postprocess
functions (drawdown / dewatering / mounding / ASR / hydroperiod / river-
seepage) do NOT call this yet -- wiring them in later needs ZERO
``export_case_to_qgis`` changes (the discovery side probes by run_id, not by
which postprocess function ran); it is a follow-up, not attempted here.

The uploaded object lands at ``s3://<runs_bucket>/<run_id>/modflow_mesh.nc``
(via ``cog_io.upload_cog`` -- generically named but scheme/content-type
agnostic, the SAME uploader every other MODFLOW COG uses) -- a SIBLING of
``plume_concentration_4326.tif``, discovered by
``export_case_to_qgis._mesh_entry_for_layer`` exactly like SFINCS's
``sfincs_map.nc`` sibling.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Sequence

from . import cog_io
from .cog_io import CogIoError
from .postprocess_modflow import (
    RUNS_BUCKET_DEFAULT,
    PostprocessMODFLOWError,
    _grid_georegistration_from_deck,
    _MF6_DRY_SENTINEL,
    _resolve_gwf_hds_path,
    _resolve_ucn_path,
)

logger = logging.getLogger("grace2_agent.workflows.modflow_mesh")

__all__ = [
    "ModflowMeshError",
    "MODFLOW_MESH_FILENAME",
    "build_modflow_ugrid_mesh_netcdf",
    "emit_modflow_mesh_artifact",
]

#: Object-key filename the QGIS-export mesh discovery probes for as a sibling
#: of the run's COG(s) -- the MODFLOW analogue of SFINCS's ``sfincs_map.nc``.
MODFLOW_MESH_FILENAME: str = "modflow_mesh.nc"

#: MF6 time unit for every deck this adapter builds (gwt_adapter.TIME_UNITS).
_MODFLOW_TIME_UNITS_CF: str = "days since 1970-01-01 00:00:00"


class ModflowMeshError(RuntimeError):
    """Raised on a mesh build/read failure. Open-set A.6 ``error_code``:

    - ``MESH_GEOREGISTRATION_UNAVAILABLE`` -- the deck could not be read for
      grid origin/cell-size (mesh geometry needs it; unlike the COG path there
      is no identity-transform fallback -- a mesh with no real coordinates is
      not useful).
    - ``MESH_HEAD_READ_FAILED`` -- the GWF head timeseries could not be read.
    - ``MESH_WRITE_FAILED`` -- the CF-UGRID NetCDF could not be written.
    - ``MESH_UPLOAD_FAILED`` -- the object-store upload failed.

    ``emit_modflow_mesh_artifact`` catches this (and everything else) and
    degrades to ``None`` -- it is the CALLER's choice whether a raised
    ``ModflowMeshError`` from the lower-level builders is fatal (it never is,
    from ``postprocess_modflow``)."""

    error_code: str = "MODFLOW_MESH_FAILED"

    def __init__(
        self, error_code: str, *, message: str | None = None, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


# --------------------------------------------------------------------------- #
# Head / concentration time series readers (own copy: needs TIMES + grids;
# the existing postprocess_modflow readers return grids only).
# --------------------------------------------------------------------------- #


def _to_2d_masked(arr: Any) -> Any:
    """Collapse a ``(nlay, nrow, ncol)`` / ``(nrow, ncol)`` array to 2D
    max-over-layers and mask the MF6 dry/inactive sentinel to NaN -- the SAME
    reduction every other MODFLOW postprocess reader applies."""
    import numpy as np  # type: ignore[import-not-found]

    a = np.asarray(arr, dtype="float64")
    if a.ndim == 3:
        a2 = np.nanmax(a, axis=0)
    elif a.ndim == 2:
        a2 = a
    else:
        a2 = np.squeeze(a)
    return np.where(np.abs(a2) > _MF6_DRY_SENTINEL, np.nan, a2)


def _read_head_time_series(hds_path: Path) -> tuple[list[float], list[Any]]:
    """Read EVERY saved GWF head step -- ``(times, 2D max-over-layers grids)``.

    Raises ``ModflowMeshError("MESH_HEAD_READ_FAILED")`` on any read failure
    (missing flopy, unreadable file, no saved timesteps)."""
    try:
        import flopy.utils  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise ModflowMeshError(
            "MESH_HEAD_READ_FAILED",
            message=f"flopy not importable: {exc}",
            details={"hds_path": str(hds_path)},
        ) from exc
    try:
        hobj = flopy.utils.HeadFile(str(hds_path))
        times = hobj.get_times()
        if not times:
            raise ModflowMeshError(
                "MESH_HEAD_READ_FAILED",
                message=f"{hds_path} carries no head timesteps",
                details={"hds_path": str(hds_path)},
            )
        grids = [_to_2d_masked(hobj.get_data(totim=t)) for t in times]
        return [float(t) for t in times], grids
    except ModflowMeshError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ModflowMeshError(
            "MESH_HEAD_READ_FAILED",
            message=f"could not read head steps from {hds_path}: {exc}",
            details={"hds_path": str(hds_path)},
        ) from exc


def _read_conc_time_series(ucn_path: Path) -> tuple[list[float], list[Any]]:
    """Read EVERY saved GWT concentration step -- ``(times, 2D grids)``.

    Raises ``ModflowMeshError("MESH_HEAD_READ_FAILED")`` on any read failure
    (reuses the head error code -- both are "the mesh's time-varying source
    data could not be read", not a distinct failure class)."""
    try:
        import flopy.utils  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise ModflowMeshError(
            "MESH_HEAD_READ_FAILED",
            message=f"flopy not importable: {exc}",
            details={"ucn_path": str(ucn_path)},
        ) from exc
    try:
        cobj = flopy.utils.HeadFile(str(ucn_path), text="CONCENTRATION")
        times = cobj.get_times()
        if not times:
            raise ModflowMeshError(
                "MESH_HEAD_READ_FAILED",
                message=f"{ucn_path} carries no concentration timesteps",
                details={"ucn_path": str(ucn_path)},
            )
        grids = [_to_2d_masked(cobj.get_data(totim=t)) for t in times]
        return [float(t) for t in times], grids
    except ModflowMeshError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ModflowMeshError(
            "MESH_HEAD_READ_FAILED",
            message=f"could not read concentration steps from {ucn_path}: {exc}",
            details={"ucn_path": str(ucn_path)},
        ) from exc


# --------------------------------------------------------------------------- #
# Pure builder: DIS grid + time-varying grids -> CF-UGRID NetCDF (unit-testable
# with no S3 / flopy / mf6 -- just numpy + xarray on synthetic inputs).
# --------------------------------------------------------------------------- #


def build_modflow_ugrid_mesh_netcdf(
    *,
    geo: dict[str, Any],
    model_crs: str,
    head_times: Sequence[float],
    head_grids: Sequence[Any],
    conc_times: Sequence[float] | None = None,
    conc_grids: Sequence[Any] | None = None,
    out_path: str | Path | None = None,
) -> Path:
    """Build a CF-1.6 / UGRID-1.0 2D mesh NetCDF from a regular DIS grid.

    Args:
        geo: ``{"xorigin", "yorigin", "delr", "delc", "nrow", "ncol"}`` --
            the SAME dict ``_grid_georegistration_from_deck`` returns (lower-
            left origin + uniform cell size, flopy row 0 = north).
        model_crs: the deck's projected CRS, e.g. ``"EPSG:32617"`` -- written
            into the ``crs`` variable's ``epsg_code`` attr (SFINCS encoding).
        head_times: cumulative model time (days -- MF6 TIME_UNITS=DAYS) per
            saved GWF head step.
        head_grids: one 2D ``(nrow, ncol)`` array per ``head_times`` entry
            (NaN off-grid/dry -- already max-over-layers reduced).
        conc_times / conc_grids: the GWT analogue; omit (``None``) for a
            GWF-only deck -- the ``concentration`` variable is then simply
            absent from the file (honest omission, not a zero-filled fake).
        out_path: destination path; a fresh temp file when omitted.

    Returns:
        The written NetCDF's path.

    Raises:
        ModflowMeshError: ``MESH_WRITE_FAILED`` on any xarray/netCDF4 failure,
            or a ``ValueError`` (surfaced as the same code) on malformed
            ``geo`` / mismatched grid shapes.
    """
    try:
        import numpy as np  # type: ignore[import-not-found]
        import xarray as xr  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise ModflowMeshError(
            "MESH_WRITE_FAILED", message=f"numpy/xarray not importable: {exc}"
        ) from exc

    try:
        nrow = int(geo["nrow"])
        ncol = int(geo["ncol"])
        delr = float(geo["delr"])
        delc = float(geo["delc"])
        xorigin = float(geo["xorigin"])
        yorigin = float(geo["yorigin"])
        if nrow <= 0 or ncol <= 0:
            raise ValueError(f"nrow/ncol must be positive (got {nrow}, {ncol})")
        if not head_times or not head_grids or len(head_times) != len(head_grids):
            raise ValueError(
                f"head_times ({len(head_times)}) and head_grids "
                f"({len(head_grids)}) must be non-empty and equal length"
            )
        if (conc_times is None) != (conc_grids is None):
            raise ValueError("conc_times and conc_grids must both be set or both omitted")
        if conc_times is not None and len(conc_times) != len(conc_grids):  # type: ignore[arg-type]
            raise ValueError(
                f"conc_times ({len(conc_times)}) and conc_grids "
                f"({len(conc_grids)}) must be equal length"  # type: ignore[arg-type]
            )

        # --- Node lattice: (nrow+1) x (ncol+1), row-major flatten ----------- #
        # flopy row 0 = north (mirrors _write_reprojected_cog's from_origin
        # convention: north = yorigin + nrow*delc).
        node_x_1d = xorigin + np.arange(ncol + 1, dtype="float64") * delr
        north = yorigin + nrow * delc
        node_y_1d = north - np.arange(nrow + 1, dtype="float64") * delc
        node_x_2d, node_y_2d = np.meshgrid(node_x_1d, node_y_1d)  # (nrow+1, ncol+1)
        node_x = node_x_2d.reshape(-1)
        node_y = node_y_2d.reshape(-1)

        def _node_id(r: Any, c: Any) -> Any:
            return r * (ncol + 1) + c

        rows = np.repeat(np.arange(nrow), ncol)
        cols = np.tile(np.arange(ncol), nrow)
        # CCW quad winding (x-east / y-north plane): bottom-left -> bottom-
        # right -> top-right -> top-left. "top" = smaller row index (north).
        face_nodes = np.stack(
            [
                _node_id(rows + 1, cols),
                _node_id(rows + 1, cols + 1),
                _node_id(rows, cols + 1),
                _node_id(rows, cols),
            ],
            axis=1,
        ).astype("int32")
        face_x = xorigin + (cols.astype("float64") + 0.5) * delr
        face_y = north - (rows.astype("float64") + 0.5) * delc
        n_faces = nrow * ncol

        def _stack_faces(grids: Sequence[Any]) -> Any:
            out = np.empty((len(grids), n_faces), dtype="float32")
            for i, g in enumerate(grids):
                g2 = np.asarray(g, dtype="float64")
                if g2.shape != (nrow, ncol):
                    raise ValueError(
                        f"grid #{i} has shape {g2.shape}, expected ({nrow}, {ncol})"
                    )
                out[i, :] = g2.reshape(-1).astype("float32")
            return out

        # --- Shared "time" axis (MDAL netCDF/UGRID reader requirement) ------ #
        # MDAL's UGRID driver only recognizes a time-varying data variable when
        # its extra dimension is LITERALLY named "time" (verified empirically
        # against libprovider_mdal.so -- a variable on a differently-named
        # dimension, even with a correct CF standard_name="time" attribute, is
        # silently dropped: datasetGroupCount stays 0). A netCDF dimension name
        # is unique per file, so head and concentration -- which the REAL spill
        # deck saves on DIFFERENT schedules (gwt_adapter: GWF head OC usually
        # ``saverecord=[("HEAD","LAST")]``, one step; GWT concentration OC
        # ``saverecord=[("CONCENTRATION", conc_save)]``, typically many steps)
        # -- share ONE "time" axis: the SORTED UNION of both quantities' saved
        # totim values. A quantity with no saved step at a given shared time
        # slot is NaN there (honest gap, never interpolated/faked) -- exactly
        # like the ``_FillValue``-masked dry/inactive cells every other
        # MODFLOW postprocess reader already produces.
        conc_times_list = list(conc_times) if conc_times is not None else []
        time_axis = sorted({float(t) for t in head_times} | {float(t) for t in conc_times_list})
        time_index = {t: i for i, t in enumerate(time_axis)}

        def _reindex_onto_time_axis(times: Sequence[float], stacked: Any) -> Any:
            out = np.full((len(time_axis), n_faces), np.nan, dtype="float32")
            for row, t in enumerate(times):
                out[time_index[float(t)], :] = stacked[row, :]
            return out

        head_stack = _reindex_onto_time_axis(head_times, _stack_faces(head_grids))

        data_vars: dict[str, tuple] = {
            "mesh2d": (
                (),
                np.int32(0),
                {
                    "cf_role": "mesh_topology",
                    "long_name": "Topology data of 2D mesh",
                    "topology_dimension": np.int32(2),
                    "node_coordinates": "mesh2d_node_x mesh2d_node_y",
                    "face_node_connectivity": "mesh2d_face_nodes",
                    "face_dimension": "nmesh2d_face",
                    "face_coordinates": "mesh2d_face_x mesh2d_face_y",
                },
            ),
            "mesh2d_node_x": (
                ("nmesh2d_node",),
                node_x,
                {"standard_name": "projection_x_coordinate", "units": "m"},
            ),
            "mesh2d_node_y": (
                ("nmesh2d_node",),
                node_y,
                {"standard_name": "projection_y_coordinate", "units": "m"},
            ),
            "mesh2d_face_x": (
                ("nmesh2d_face",),
                face_x,
                {"standard_name": "projection_x_coordinate", "units": "m"},
            ),
            "mesh2d_face_y": (
                ("nmesh2d_face",),
                face_y,
                {"standard_name": "projection_y_coordinate", "units": "m"},
            ),
            "mesh2d_face_nodes": (
                ("nmesh2d_face", "max_nmesh2d_face_nodes"),
                face_nodes,
                {
                    "cf_role": "face_node_connectivity",
                    "long_name": "Vertex nodes of mesh faces (counterclockwise)",
                    "start_index": np.int32(0),
                },
            ),
            "crs": (
                (),
                np.int32(0),
                {"epsg_code": str(model_crs), "grid_mapping_name": "unknown"},
            ),
            "time": (
                ("time",),
                np.asarray(time_axis, dtype="float64"),
                {
                    "standard_name": "time",
                    "units": _MODFLOW_TIME_UNITS_CF,
                    "calendar": "standard",
                    "axis": "T",
                },
            ),
            "head": (
                ("time", "nmesh2d_face"),
                head_stack,
                {
                    "mesh": "mesh2d",
                    "location": "face",
                    "coordinates": "mesh2d_face_x mesh2d_face_y",
                    "units": "m",
                    "long_name": "Simulated Head",
                    "_FillValue": np.float32(np.nan),
                },
            ),
        }

        if conc_times is not None and conc_grids is not None:
            conc_stack = _reindex_onto_time_axis(conc_times_list, _stack_faces(conc_grids))
            data_vars["concentration"] = (
                ("time", "nmesh2d_face"),
                conc_stack,
                {
                    "mesh": "mesh2d",
                    "location": "face",
                    "coordinates": "mesh2d_face_x mesh2d_face_y",
                    "units": "mg/L",
                    "long_name": "Simulated Concentration",
                    "_FillValue": np.float32(np.nan),
                },
            )

        ds = xr.Dataset(
            data_vars,
            attrs={
                # The EXACT literal MDAL's netCDF/UGRID driver's own writer
                # emits (verified via strings(1) against libprovider_mdal.so);
                # kept byte-identical rather than a newer CF version string on
                # the theory that the reader's OWN convention string round-
                # trips most reliably.
                "Conventions": "CF-1.6 UGRID-1.0",
                "title": "MODFLOW 6 groundwater mesh (GRACE-2/TRID3NT)",
                "source": "grace2_agent.workflows.modflow_mesh",
            },
        )

        if out_path is not None:
            dest = Path(out_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
        else:
            tmpdir = Path(tempfile.mkdtemp(prefix="grace2_modflowmesh_"))
            dest = tmpdir / MODFLOW_MESH_FILENAME
        ds.to_netcdf(str(dest))
        return dest
    except ModflowMeshError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ModflowMeshError(
            "MESH_WRITE_FAILED", message=f"could not build UGRID mesh NetCDF: {exc}"
        ) from exc


# --------------------------------------------------------------------------- #
# Orchestration: read run outputs -> build -> upload. Best-effort (see module
# docstring "Wiring" section) -- NEVER raises; a failure returns None.
# --------------------------------------------------------------------------- #


def emit_modflow_mesh_artifact(
    run_outputs_uri: str,
    *,
    run_id: str,
    model_crs: str,
    deck_dir: str | None,
    runs_bucket: str | None = None,
    include_concentration: bool = True,
) -> str | None:
    """Build + upload the run's UGRID mesh NetCDF; ``None`` on ANY failure.

    Best-effort / additive (mirrors ``_dispatch_publish_layer``): missing deck
    georegistration, an unreadable head file, or an upload failure are logged
    warnings, never a raised exception -- a mesh is a bonus artifact, not a
    reason to fail the plume/head COG the caller already produced.

    Args:
        run_outputs_uri: the run's output location (passed straight through
            to the SAME resolvers ``postprocess_modflow`` uses).
        run_id: the run identifier the mesh is keyed under
            (``<run_id>/modflow_mesh.nc``).
        model_crs: the deck's projected CRS (OQ-MOD-3 handoff).
        deck_dir: the local deck dir for georegistration -- REQUIRED here
            (unlike the COG path there is no identity-transform fallback; a
            mesh needs real node coordinates or it is not worth building).
        runs_bucket: optional runs-bucket override.
        include_concentration: when True (default), also read + embed the GWT
            concentration timeseries if ``gwt_model.ucn`` is found; a GWF-only
            archetype deck (no UCN) degrades to a head-only mesh, not a
            failure.

    Returns:
        The uploaded mesh's object URI, or ``None`` if anything went wrong.
    """
    nc_path: Path | None = None
    try:
        geo = _grid_georegistration_from_deck(deck_dir)
        if geo is None:
            logger.warning(
                "modflow mesh SKIPPED run_id=%s: no deck georegistration "
                "available (deck_dir=%r)",
                run_id,
                deck_dir,
            )
            return None

        hds_path = _resolve_gwf_hds_path(run_outputs_uri)
        head_times, head_grids = _read_head_time_series(hds_path)

        conc_times: list[float] | None = None
        conc_grids: list[Any] | None = None
        if include_concentration:
            try:
                ucn_path = _resolve_ucn_path(run_outputs_uri)
                conc_times, conc_grids = _read_conc_time_series(ucn_path)
            except (PostprocessMODFLOWError, ModflowMeshError) as exc:
                # No UCN (GWF-only archetype) or an unreadable one -- honest
                # omission, never fatal to the head-only mesh.
                logger.info(
                    "modflow mesh run_id=%s: no concentration timeseries (%s) "
                    "-- head-only mesh",
                    run_id,
                    exc,
                )

        nc_path = build_modflow_ugrid_mesh_netcdf(
            geo=geo,
            model_crs=model_crs,
            head_times=head_times,
            head_grids=head_grids,
            conc_times=conc_times,
            conc_grids=conc_grids,
        )

        try:
            mesh_uri = cog_io.upload_cog(
                nc_path,
                run_id,
                runs_bucket,
                dest_filename=MODFLOW_MESH_FILENAME,
                content_type="application/x-netcdf",
                gs_backend="fsspec",
                gs_fallback_to_file=True,
                runs_bucket_default=RUNS_BUCKET_DEFAULT,
                log_label="MODFLOW UGRID mesh",
            )
        except CogIoError as exc:
            raise ModflowMeshError(
                "MESH_UPLOAD_FAILED", message=exc.message, details=dict(exc.details)
            ) from exc

        # cog_io.upload_cog's local-dev degrade (no bucket configured under
        # the gs scheme) returns f"file://{nc_path}" -- the SAME temp path,
        # reused AS the persistent local store (byte-identical to the COG
        # path's own local-fallback convention: _write_reprojected_cog's
        # output is likewise never unlinked when the s3/gs upload degrades to
        # file://). Deleting it below would silently break the URI just
        # returned to the caller -- only clean up when the upload made an
        # independent copy elsewhere.
        if mesh_uri == f"file://{nc_path}":
            nc_path = None

        logger.info(
            "modflow mesh emitted run_id=%s uri=%s head_steps=%d conc_steps=%d",
            run_id,
            mesh_uri,
            len(head_times),
            len(conc_times) if conc_times else 0,
        )
        return mesh_uri
    except Exception as exc:  # noqa: BLE001 -- best-effort, see docstring
        logger.warning(
            "modflow mesh emission FAILED (non-fatal) run_id=%s: %s", run_id, exc
        )
        return None
    finally:
        if nc_path is not None:
            try:
                os.unlink(nc_path)
            except OSError:
                pass
