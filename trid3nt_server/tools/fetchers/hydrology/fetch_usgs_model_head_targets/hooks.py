"""usgs_model_head_targets hooks: the published water-level targets of a model archive, as points.

``build_request`` refuses a box outside the model grid and GETs the ancillary archive whole; ``parse_response``
joins the control file's observation table to the tables its targets were written from, keeps the box, and
refuses an empty answer typed."""

from __future__ import annotations

import csv
import io
import zipfile
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router import hooks as _hooks
from ..._router.errors import router_empty_error, router_input_error, router_upstream_error

__all__ = ["build_request", "parse_response", "PERIODS"]

_ARCHIVE = "des_moines"
_OBS_DATA = "pestfiles/tr_mf6_sim/input/pest_dmww_tr6.obs_data.csv"
_WELLS = "scripts/run_model/output/DMWW_gw_wl_obs.csv"
_NWIS = "scripts/run_model/output/DMWW_gw_nwis_wl_obs.csv"
#: The control file's group of water-level ELEVATION targets; its other groups are changes, flows and rates.
_GROUP = "wl_alt_obs"
#: The model's transient stress periods start here; a reading before it is matched to the steady first period.
_TRANSIENT_FROM = "1980-01-01"
PERIODS = ("steady", "transient")


def _period(params: dict[str, Any], spec: SourceSpec) -> str | None:
    stated = str(params.get("period") or "").strip().lower()
    if not stated:
        return None
    if stated not in PERIODS:
        raise router_input_error(spec.error_code_prefix,
                                 f"period={params.get('period')!r} names no stress period of the model; it has "
                                 f"{' and '.join(PERIODS)}", spec.input_error_suffix)
    return stated


@_hooks.register_hook("usgs_model_head_targets.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """GET the ancillary archive; a box the model grid does not wholly hold refuses before the download."""
    from .._usgs_model_grid import ARCHIVES, covering_archive

    _period(params, spec)
    bbox = tuple(round(float(v), 6) for v in params["bbox"])
    try:
        held = covering_archive(spec, bbox)
    except Exception as exc:  # noqa: BLE001 - both refusals name the grid; this row reaches one
        raise router_input_error(spec.error_code_prefix, str(exc), spec.input_error_suffix) from exc
    if held != _ARCHIVE:
        raise router_input_error(spec.error_code_prefix,
                                 f"bbox {list(bbox)} is held by the {ARCHIVES[held]['label']} grid, and this row "
                                 f"reads the targets of the {ARCHIVES[_ARCHIVE]['label']} model only",
                                 spec.input_error_suffix)
    # no params: a params mapping, even an empty one, replaces the query the archive's url is addressed by
    url = spec.endpoints["des_moines_ancillary"].url
    return [_hooks.RequestPlan(url=url, headers={"User-Agent": spec.auth.user_agent})]


def _rows(zf: zipfile.ZipFile, member: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(zf.read(member).decode("utf-8"))))


def _iso(stamp: str) -> str:
    return stamp.strip().replace("/", "-")


@_hooks.register_hook("usgs_model_head_targets.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Each elevation target inside the box as a point on NAVD 88 in metres; none inside refuses EMPTY."""
    from rasterio.warp import transform

    from .._usgs_model_grid import ARCHIVES

    sc = spec.error_code_prefix
    asked = _period(params, spec)
    try:
        with zipfile.ZipFile(io.BytesIO(bodies[0] if bodies else b"")) as zf:
            targets = {row["obsnme"]: row for row in _rows(zf, _OBS_DATA) if row["obgnme"] == _GROUP}
            sited = [(row["obsnme"], row["bore_id"], row["xcoord"], row["ycoord"], row["layer"], row["wl_date"])
                     for row in _rows(zf, _WELLS)]
            sited += [(row["obsnam"], row["bore_id"], row["xcoord"], row["ycoord"], row["layer"], row["EOM_date"])
                      for row in _rows(zf, _NWIS)]
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, csv.Error) as exc:
        raise router_upstream_error(sc, f"the archive does not hold the target tables it publishes: {exc}")
    placed = [row for row in sited if row[0] in targets]
    missing = set(targets) - {row[0] for row in placed}
    if missing:
        raise router_upstream_error(sc, f"{len(missing)} targets of the control file are sited in neither table "
                                        f"it was written from: {sorted(missing)[:5]}")
    lons, lats = transform(ARCHIVES[_ARCHIVE]["crs"], "EPSG:4326",
                           [float(row[2]) for row in placed], [float(row[3]) for row in placed])
    west, south, east, north = (float(v) for v in params["bbox"])
    feats: list[dict[str, Any]] = []
    for (name, bore, _x, _y, layer, stamp), lon, lat in zip(placed, lons, lats):
        when = _iso(stamp)
        period = "steady" if when < _TRANSIENT_FROM else "transient"
        if not (west <= lon <= east and south <= lat <= north) or (asked and period != asked):
            continue
        target = targets[name]
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "site_id": bore, "target": name, "database": name.split("_")[1],
                "value": float(target["obsval"]), "unit": "m", "quantity": "groundwater_head",
                "vertical_datum": ARCHIVES[_ARCHIVE]["datum"], "datetime": when, "model_layer": int(layer),
                "weight": float(target["weight"]), "group": target["obgnme"], "period": period,
            },
        })
    if not feats:
        raise router_empty_error(sc, f"no {asked + ' ' if asked else ''}water-level target of the "
                                     f"{ARCHIVES[_ARCHIVE]['label']} model lies in bbox {params['bbox']}",
                                 spec.empty_error_suffix)
    return feats
