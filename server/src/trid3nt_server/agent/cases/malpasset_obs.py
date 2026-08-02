"""Build the Malpasset dam-break OBSERVATION layers from ``observations.json``.

Turns the transcribed Malpasset validation observations (police high-water-mark
survey points, electrical-transformer wave-arrival times, physical-model gauges)
into FlatGeobuf point layers the pairing/skill path consumes. Pure + offline
(geopandas + shapely only; no network, no S3).

WHAT IT PRODUCES
----------------
- ``malpasset_police_hwm.fgb`` -- the 17 police survey points (P1-P17), each
  carrying the surveyed MAX water-surface ELEVATION as ``elev_m`` (the field
  ``extract_model_at_observations`` auto-detects), a ``quantity=
  "water_surface_elevation"`` stamp (so it pairs like-for-like against a TELEMAC
  max-WSE raster -- NO depth/DEM conversion), and a ``vertical_datum`` column.
  This is the layer the L2 harness scores against.
- ``malpasset_transformers.fgb`` -- the 3 transformer wave-ARRIVAL-TIME points
  (A/B/C). Built + saved for provenance, but NOT consumed by the current
  pairing path: arrival-time-vs-WSE is not a like-for-like raster pairing (it
  needs a time-of-arrival raster the postprocess does not yet emit), so timing
  validation is recorded as FUTURE WORK, not scored by the harness today.
- ``malpasset_gauges.fgb`` (optional) -- the 9 physical-model gauges (G6-G14)
  with both ``ws_lab_m`` (max WSE) and ``at_lab_s`` (arrival time). Emitted for
  completeness; the WSE column pairs the same way the police points do.

CRS DISCIPLINE (the honesty caveat)
-----------------------------------
The Malpasset observation coordinates AND the bundled TELEMAC mesh
(``geo_malpasset-*.slf``) share a LOCAL planar metric map frame (metres), NOT a
named EPSG projection (confirmed by Biscarini et al. 2016 + the ANUGA mesh
dam-node cross-check; see ``SOURCES.md`` section 6). GDAL/rasterio/geopandas
still require *some* CRS label to write a file and to reproject during pairing,
so BOTH these observation layers AND the TELEMAC WSE raster are stamped with the
SAME placeholder projected EPSG (:data:`MALPASSET_MESH_EPSG`). Because both sides
carry the IDENTICAL stamp, the pairing tool's ``obs.to_crs(model_crs)`` is an
exact identity (zero reprojection distortion) and WSE pairs at the true surveyed
coordinates. The stamp is a PLACEHOLDER for GDAL's sake ONLY: the coordinates are
the local Malpasset frame, so these layers do NOT overlay correctly on a
real-world basemap without the (unpublished) local->UTM anchor. This caveat is
carried in the returned summary and should be surfaced by any consumer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "MALPASSET_MESH_EPSG",
    "MALPASSET_VERTICAL_DATUM",
    "MALPASSET_CRS_CAVEAT",
    "load_observations",
    "build_police_gdf",
    "build_transformer_gdf",
    "build_gauge_gdf",
    "build_malpasset_obs_layers",
]

#: PLACEHOLDER projected EPSG the Malpasset local metric frame is stamped with.
#: The sources cite UTM zone 32N as the rough real-world region (dam ~43.5072 N,
#: 6.7539 E), so 32632 is used as the label -- but the coordinates are the LOCAL
#: map frame (origin near the study-area corner), NOT true UTM 32N eastings /
#: northings. Used identically on the obs layers AND the TELEMAC WSE raster so
#: pairing is an exact identity. See :data:`MALPASSET_CRS_CAVEAT`.
MALPASSET_MESH_EPSG: int = 32632

#: Vertical datum of the surveyed water levels (French Nivellement General de la
#: France). Not restated per-table in the source; it is the standard datum for
#: this dataset (reservoir initial free surface ~100 m NGF). The bundled TELEMAC
#: mesh bed + FREE SURFACE are in this same reference (bed -20..100 m, reservoir
#: 100 m), so a TELEMAC-WSE-vs-police-HWM pairing is like-for-like in NGF.
MALPASSET_VERTICAL_DATUM: str = "NGF"

MALPASSET_CRS_CAVEAT: str = (
    f"Coordinates are the LOCAL Malpasset map frame (metres), stamped EPSG:"
    f"{MALPASSET_MESH_EPSG} as a GDAL placeholder ONLY -- they are NOT true UTM "
    f"32N eastings/northings. This layer shares the identical stamp with the "
    f"TELEMAC mesh/WSE raster so pairing is an exact identity, but it does NOT "
    f"overlay on a real basemap without the (unpublished) local->UTM anchor."
)


def load_observations(observations: str | Path | dict[str, Any]) -> dict[str, Any]:
    """Return the observations dict (pass-through a dict, else read the JSON path)."""
    if isinstance(observations, dict):
        return observations
    path = Path(observations)
    if not path.is_file():
        raise FileNotFoundError(f"observations file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _vertical_datum(obs: dict[str, Any]) -> str:
    """Best-effort vertical-datum label from the observations file, else NGF.

    The source ``vertical_reference.datum_note`` names NGF but not as a bare
    field; we look for it and fall back to the module default (never fabricate an
    EPSG-vertical code the source did not state)."""
    vr = obs.get("vertical_reference") or {}
    note = str(vr.get("datum_note") or "")
    if "NGF" in note.upper() or "NIVELLEMENT GENERAL" in note.upper():
        return MALPASSET_VERTICAL_DATUM
    return MALPASSET_VERTICAL_DATUM


def build_police_gdf(observations: str | Path | dict[str, Any]):
    """Build the police high-water-mark GeoDataFrame (17 points, WSE in metres).

    Columns: ``obs_id`` (P1-P17), ``elev_m`` (surveyed max WSE, the observed
    field the pairing tool auto-detects), ``ws_obs_m`` (provenance copy),
    ``bank``, ``quantity="water_surface_elevation"``, ``vertical_datum``,
    ``units="m"``. CRS = :data:`MALPASSET_MESH_EPSG` (placeholder; see caveat).
    """
    import geopandas as gpd
    from shapely.geometry import Point

    obs = load_observations(observations)
    block = obs.get("police_survey_points") or {}
    pts = block.get("points") or []
    if not pts:
        raise ValueError("observations.json has no police_survey_points.points")
    vdatum = _vertical_datum(obs)

    rows: dict[str, list[Any]] = {
        "obs_id": [], "elev_m": [], "ws_obs_m": [], "bank": [],
        "quantity": [], "vertical_datum": [], "units": [],
    }
    geoms = []
    for p in pts:
        ws = p.get("ws_obs_m")
        rows["obs_id"].append(str(p["id"]))
        rows["elev_m"].append(None if ws is None else float(ws))
        rows["ws_obs_m"].append(None if ws is None else float(ws))
        rows["bank"].append(p.get("bank"))
        rows["quantity"].append("water_surface_elevation")
        rows["vertical_datum"].append(vdatum)
        rows["units"].append("m")
        geoms.append(Point(float(p["x_m"]), float(p["y_m"])))
    return gpd.GeoDataFrame(rows, geometry=geoms, crs=f"EPSG:{MALPASSET_MESH_EPSG}")


def build_transformer_gdf(observations: str | Path | dict[str, Any]):
    """Build the transformer wave-arrival-time GeoDataFrame (3 points, seconds).

    Columns: ``obs_id`` (A/B/C), ``at_obs_s`` (adopted primary arrival time),
    ``at_obs_s_alt_tuflow`` (the recorded TUFLOW variant, or None),
    ``quantity="wave_arrival_time"``, ``units="s"``. NOT consumed by the current
    WSE pairing path -- saved for future arrival-time validation.
    """
    import geopandas as gpd
    from shapely.geometry import Point

    obs = load_observations(observations)
    block = obs.get("transformers") or {}
    pts = block.get("points") or []
    if not pts:
        raise ValueError("observations.json has no transformers.points")

    rows: dict[str, list[Any]] = {
        "obs_id": [], "at_obs_s": [], "at_obs_s_alt_tuflow": [],
        "quantity": [], "units": [],
    }
    geoms = []
    for p in pts:
        at = p.get("at_obs_s")
        rows["obs_id"].append(str(p["id"]))
        rows["at_obs_s"].append(None if at is None else float(at))
        alt = p.get("at_obs_s_alt_tuflow")
        rows["at_obs_s_alt_tuflow"].append(None if alt is None else float(alt))
        rows["quantity"].append("wave_arrival_time")
        rows["units"].append("s")
        geoms.append(Point(float(p["x_m"]), float(p["y_m"])))
    return gpd.GeoDataFrame(rows, geometry=geoms, crs=f"EPSG:{MALPASSET_MESH_EPSG}")


def build_gauge_gdf(observations: str | Path | dict[str, Any]):
    """Build the 1:400 physical-model gauge GeoDataFrame (9 points, G6-G14).

    Columns: ``obs_id``, ``elev_m`` (== ``ws_lab_m`` max WSE, pairs like the
    police points), ``ws_lab_m``, ``at_lab_s`` (arrival time),
    ``quantity="water_surface_elevation"``, ``vertical_datum``, ``units="m"``.
    Returns ``None`` when the block is absent (optional layer).
    """
    import geopandas as gpd
    from shapely.geometry import Point

    obs = load_observations(observations)
    block = obs.get("physical_model_gauges") or {}
    pts = block.get("points") or []
    if not pts:
        return None
    vdatum = _vertical_datum(obs)

    rows: dict[str, list[Any]] = {
        "obs_id": [], "elev_m": [], "ws_lab_m": [], "at_lab_s": [],
        "quantity": [], "vertical_datum": [], "units": [],
    }
    geoms = []
    for p in pts:
        ws = p.get("ws_lab_m")
        rows["obs_id"].append(str(p["id"]))
        rows["elev_m"].append(None if ws is None else float(ws))
        rows["ws_lab_m"].append(None if ws is None else float(ws))
        at = p.get("at_lab_s")
        rows["at_lab_s"].append(None if at is None else float(at))
        rows["quantity"].append("water_surface_elevation")
        rows["vertical_datum"].append(vdatum)
        rows["units"].append("m")
        geoms.append(Point(float(p["x_m"]), float(p["y_m"])))
    return gpd.GeoDataFrame(rows, geometry=geoms, crs=f"EPSG:{MALPASSET_MESH_EPSG}")


def build_malpasset_obs_layers(
    observations: str | Path | dict[str, Any],
    out_dir: str | Path,
    *,
    include_gauges: bool = True,
) -> dict[str, Any]:
    """Write the Malpasset observation FlatGeobuf layers into ``out_dir``.

    Returns a summary dict: the written file paths, per-layer feature counts, the
    stamped CRS + vertical datum, and the CRS caveat. Never fabricates a value --
    a missing ``ws_obs_m`` is written as null (None), never guessed.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    police = build_police_gdf(observations)
    police_path = out / "malpasset_police_hwm.fgb"
    police.to_file(police_path, driver="FlatGeobuf", engine="pyogrio")

    transformers = build_transformer_gdf(observations)
    transformers_path = out / "malpasset_transformers.fgb"
    transformers.to_file(transformers_path, driver="FlatGeobuf", engine="pyogrio")

    summary: dict[str, Any] = {
        "police_fgb": str(police_path),
        "n_police": int(len(police)),
        "transformers_fgb": str(transformers_path),
        "n_transformers": int(len(transformers)),
        "mesh_epsg": MALPASSET_MESH_EPSG,
        "vertical_datum": MALPASSET_VERTICAL_DATUM,
        "police_quantity": "water_surface_elevation",
        "transformer_quantity": "wave_arrival_time",
        "transformer_scored": False,
        "transformer_note": (
            "arrival-time-vs-WSE is not a like-for-like raster pairing; saved for "
            "FUTURE arrival-time validation (needs a time-of-arrival raster)"
        ),
        "crs_caveat": MALPASSET_CRS_CAVEAT,
    }

    if include_gauges:
        gauges = build_gauge_gdf(observations)
        if gauges is not None:
            gauges_path = out / "malpasset_gauges.fgb"
            gauges.to_file(gauges_path, driver="FlatGeobuf", engine="pyogrio")
            summary["gauges_fgb"] = str(gauges_path)
            summary["n_gauges"] = int(len(gauges))

    return summary


def _cli(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Build Malpasset observation FGB layers.")
    ap.add_argument(
        "--observations",
        default="data/cases/malpasset/observations.json",
        help="path to observations.json (default: data/cases/malpasset/observations.json)",
    )
    ap.add_argument(
        "--out-dir",
        default="data/cases/malpasset/obs_layers",
        help="output directory for the FlatGeobuf layers",
    )
    args = ap.parse_args(argv)
    summary = build_malpasset_obs_layers(args.observations, args.out_dir)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
