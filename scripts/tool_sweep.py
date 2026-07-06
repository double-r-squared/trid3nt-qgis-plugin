#!/usr/bin/env python3
"""tool_sweep.py -- sequential direct-execution sweep of EVERY registered tool.

Layer 1 of the full local tool audit (2026-07-06): calls each tool's registered
fn directly (the sanctioned test path -- the registry deliberately keeps fn
unwrapped) with schema-introspected args and a SMALL AOI, classifies:

  PASS      tool returned without raising
  KEY       tool needs an API key / credential (earmarked for later)
  FAIL      tool raised (real local bug or data-source outage)
  SKIP-ARGS required parameter the arg-generator cannot fabricate
  TIMEOUT   exceeded the per-tool budget

Resumable: results append to docs/reports/tool-sweep-results.jsonl and tools
already present are skipped, so re-running continues the sweep. The markdown
checklist regenerates from the JSONL each run.

Usage:  venvs/agent/bin/python scripts/tool_sweep.py [--only NAME] [--limit N]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import importlib
import inspect
import json
import pkgutil
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "docs" / "reports" / "tool-sweep-results.jsonl"
CHECKLIST = REPO / "docs" / "reports" / "tool-sweep-checklist.md"

# Small AOI: ~3km box over downtown Tampa (fast fetches, tiny sims).
BBOX = (-82.47, 27.94, -82.44, 27.97)
LON, LAT = -82.455, 27.955

# Recent 2-day window for time-ranged fetchers; a fixed known-good archive
# date for satellite archive tools.
END = dt.date.today()
START = END - dt.timedelta(days=2)

KEY_MARKERS = (
    "credential", "api key", "api_key", "apikey", "token required",
    "missing key", "secret", "unauthorized", "401", "403 client error",
)

TIMEOUTS = {"model_": 900, "run_": 900, "solve": 900}
DEFAULT_TIMEOUT = 180

D30 = (END - dt.timedelta(days=30)).isoformat()
D45 = (END - dt.timedelta(days=45)).isoformat()
YDAY = (END - dt.timedelta(days=1)).isoformat()

# Pass 1.5 curated args: pass-1 gave every tool the same 3km Tampa box + 2-day
# window, so tools whose data genuinely is not there (earthquakes, snow, tide
# stations, 5-day-revisit imagery...) returned honest empty errors. Each entry
# is merged OVER the generated args; a tool listed here that previously
# SKIP-ARGSed now runs.
OVERRIDES: dict[str, dict] = {
    # no-data-in-AOI -> a bbox/window where the source has data
    "fetch_usgs_earthquakes": {"bbox": (-119.0, 33.5, -116.5, 35.5), "start_date": D30, "min_magnitude": 2.0},
    "fetch_tsunami_events": {"bbox": (-165.0, 50.0, -145.0, 62.0)},
    "fetch_usgs_volcano_alerts": {"bbox": (-123.5, 45.5, -120.5, 47.5)},
    "fetch_snotel_snow": {"bbox": (-106.8, 39.0, -105.2, 40.5)},
    "fetch_raws_weather": {"bbox": (-114.9, 46.4, -113.3, 47.6)},
    "fetch_asos_metar": {"bbox": (-82.70, 27.85, -82.35, 28.10)},
    "fetch_noaa_coops_tides": {"bbox": (-82.9, 27.5, -82.4, 28.0)},
    "fetch_noaa_coops_currents": {"bbox": (-82.9, 27.5, -82.4, 28.0)},
    "fetch_noaa_sst": {"bbox": (-84.0, 26.5, -83.0, 27.5)},
    "fetch_soilgrids": {"bbox": (-93.8, 41.9, -93.6, 42.1)},
    # imagery: widen the window past the revisit cadence
    "fetch_modis_lst": {"start_date": D30},
    "fetch_landsat_imagery": {"start_date": D45},
    "fetch_sentinel2_truecolor": {"start_date": D30},
    "fetch_sentinel1_sar": {"start_date": D30},
    "compute_ndvi": {"start_date": D30},
    "digitize_water_body": {"bbox": (-82.78, 28.07, -82.71, 28.16), "start_date": D30},
    "fetch_chirps_precipitation": {"date": (END - dt.timedelta(days=60)).replace(day=1).isoformat(), "period": "monthly"},
    # required/enum params
    "fetch_overpass_pois": {"tag": "amenity=hospital"},
    "fetch_nhdplus_nldi_navigate": {"seed_point": (LON, LAT)},
    "fetch_administrative_boundaries": {"level": "county"},
    "fetch_gridmet": {"variable": "pr"},
    "fetch_era5_reanalysis": {"variable": "2m_temperature"},
    "fetch_hifld_critical_infrastructure": {"facility_type": "hospitals"},
    "fetch_storm_events_db": {"year": 2024, "state": "FL"},
    "fetch_nws_event": {"area": "FL"},
    "fetch_nws_alerts_conus": {"area": "FL"},
    "catalog_search": {"topic": "flood"},
    "list_tools_in_category": {"category_id": "data_fetch"},
    "lookup_precip_return_period": {"return_period_years": 100, "duration_hours": 24},
    "compute_overtopping": {"hs_m": 2.0, "tp_s": 8.0, "crest_freeboard_m": 1.0, "slope": 0.25},
    "compute_wave_nomograph": {"wind_speed_ms": 20.0, "fetch_km": 50.0},
    "code_exec_request": {"python_code": "print(21*2)"},
    "describe_qgis_algorithm": {"algorithm_id": "native:buffer"},
    "fetch_ebird_observations": {"species_code": "baleag"},
    "fetch_inaturalist_observations": {"taxon_id": 6930},
    "fetch_iucn_red_list_range": {"species_name": "Puma concolor"},
    "fetch_wfigs_incident": {"incident_name": "Park Fire"},
    "run_model_satellite_fire_animation": {"incident_name": "Park Fire"},
    # satellite animation: explicit small window yesterday + a bbox that sees data
    "fetch_goes_archive_animation": {"bbox": (-121.5, 39.5, -121.0, 40.0), "start_utc": f"{YDAY}T18:00", "end_utc": f"{YDAY}T18:30"},
    "fetch_goes_blend_animation": {"start_utc": f"{YDAY}T18:00", "end_utc": f"{YDAY}T18:30"},
    "fetch_goes_active_fire": {"bbox": (-121.5, 39.5, -121.0, 40.0)},
    "fetch_glm_lightning": {"bbox": (-90.0, 25.0, -85.0, 30.0), "start_utc": f"{YDAY}T18:00", "end_utc": f"{YDAY}T18:20"},
    "run_model_glm_lightning_animation": {"bbox": (-90.0, 25.0, -85.0, 30.0)},
    "run_model_groundwater_contamination_scenario": {
        "article_text": "A tanker spill released benzene near Tampa, Florida on 2026-06-01, "
                        "contaminating shallow groundwater around 27.955N 82.455W per county officials."
    },
}

OVERRIDES.update({
    "lookup_precip_return_period": {"return_period_years": 100, "duration_hours": 24, "location": (LAT, LON)},
    "list_tools_in_category": {"category_id": "hazard_modeling"},
    "fetch_overpass_pois": {"tag": "amenity=hospital", "bbox": (-82.55, 27.90, -82.40, 28.00)},
    "fetch_gridmet": {"variable": "pr", "bbox": (-82.7, 27.8, -82.2, 28.2)},
    "fetch_climate_normals": {"bbox": (-82.9, 27.6, -82.1, 28.3)},
    "digitize_water_body": {"bbox": (-82.78, 28.07, -82.71, 28.16), "start_date": (END - dt.timedelta(days=90)).isoformat()},
    "run_model_groundwater_contamination_scenario": {
        "article_text": "A tanker spill released approximately 5,000 gallons of benzene near Tampa, "
                        "Florida on 2026-06-01, contaminating shallow groundwater around 27.955N 82.455W "
                        "per county officials."
    },
})

# Pass 2: chain REAL layer URIs (prefetched once, cache-warm) into the
# layer-input tools. Param-NAME keyed, applied by _guess_arg fallback.
CHAIN_PARAM_MAP_SPEC = {
    "dem": ("dem_uri", "raster_uri", "value_raster_uri", "base_layer_uri",
            "source_layer_uri", "layer_uri", "hazard_raster_uri", "imagery_uri"),
    "landcover": ("landcover_uri", "overlay_layer_uri"),
    "counties": ("zone_layer_uri", "zone_input_uri", "polygon_uri", "vector_uri",
                 "cutter_uri", "target_uri", "fields_layer_uri", "zones_uri",
                 "assets_uri", "boundaries_uri"),
    "quakes": ("points_uri",),
}
CHAINED: dict[str, str] = {}   # param name -> real uri (filled by prefetch)

STATIC_CHAIN = {
    "line": {"type": "LineString",
             "coordinates": [[-82.47, 27.94], [-82.44, 27.97]]},
    "property": "ALAND",
    "threshold": 1.0,
    "layer_id": "sweep-layer",
    "classes": [11],
    "algorithm": "native:buffer",
    "params": {},
    "entry_id": None,   # filled from catalog_search at prefetch
    "run_id": None,     # filled from MinIO at prefetch
}


def prefetch_chain(tools) -> None:
    """Fetch the four canonical layers (cache-warm after pass 1) + ids."""
    def uri_of(result):
        return getattr(result, "uri", None) or str(result)
    plan = [
        ("dem", "fetch_copernicus_dem", {"bbox": BBOX}),
        ("landcover", "fetch_esri_landcover_10m", {"bbox": BBOX}),
        ("counties", "fetch_administrative_boundaries", {"bbox": BBOX, "level": "county"}),
        ("quakes", "fetch_usgs_earthquakes", OVERRIDES["fetch_usgs_earthquakes"]),
    ]
    for key, tname, kw in plan:
        try:
            u = uri_of(tools[tname].fn(**kw))
            for pname in CHAIN_PARAM_MAP_SPEC[key]:
                CHAINED[pname] = u
            print(f"[chain] {key} <- {tname}: {u[:80]}")
        except Exception as exc:
            print(f"[chain] {key} PREFETCH FAILED ({type(exc).__name__}: {exc})")
    try:
        entries = tools["catalog_search"].fn(topic="flood")
        if entries:
            STATIC_CHAIN["entry_id"] = entries[0].get("id") or entries[0].get("entry_id")
            print(f"[chain] entry_id <- {STATIC_CHAIN['entry_id']}")
    except Exception as exc:
        print(f"[chain] entry_id failed: {exc}")
    try:
        import boto3, os
        s3 = boto3.client("s3")
        pages = s3.list_objects_v2(Bucket="trid3nt-runs", Delimiter="/", MaxKeys=50)
        runs = [p["Prefix"].strip("/") for p in pages.get("CommonPrefixes", [])]
        if runs:
            STATIC_CHAIN["run_id"] = runs[-1]
            print(f"[chain] run_id <- {runs[-1]}")
    except Exception as exc:
        print(f"[chain] run_id failed: {exc}")


TIMEOUT_OVERRIDES = {
    "fetch_storm_events_db": 420,
    "fetch_climate_normals": 420,
    "fetch_population": 420,
    "compute_canopy_height": 600,
    "fetch_goes_archive_animation": 420,
    "fetch_goes_blend_animation": 420,
    "fetch_nws_alerts_conus": 300,
}


def _timeout_for(name: str) -> int:
    if name in TIMEOUT_OVERRIDES:
        return TIMEOUT_OVERRIDES[name]
    for prefix, t in TIMEOUTS.items():
        if name.startswith(prefix) or prefix in name:
            return t
    return DEFAULT_TIMEOUT


def _guess_arg(pname: str, ann: str):
    """Heuristic value for a parameter by name. Returns (ok, value)."""
    p = pname.lower()
    if p == "bbox" or p.endswith("_bbox"):
        return True, BBOX
    if p in ("lon", "longitude", "x"):
        return True, LON
    if p in ("lat", "latitude", "y"):
        return True, LAT
    if "start" in p and ("date" in p or "time" in p):
        return True, START.isoformat()
    if "end" in p and ("date" in p or "time" in p):
        return True, END.isoformat()
    if p in ("date", "day"):
        return True, START.isoformat()
    if "case_id" in p:
        return True, "sweep-case"
    if p in ("place", "query", "location", "place_name", "address"):
        return True, "Tampa, Florida"
    if p in ("state", "state_abbr"):
        return True, "FL"
    if pname in CHAINED:
        return True, CHAINED[pname]
    if pname in STATIC_CHAIN and STATIC_CHAIN[pname] is not None:
        return True, STATIC_CHAIN[pname]
    return False, None


def build_args(fn) -> tuple[dict, list[str]]:
    """Kwargs for fn from its signature. Returns (kwargs, unfillable-required)."""
    kwargs: dict = {}
    missing: list[str] = []
    overrides = OVERRIDES.get(getattr(fn, "__tool_name__", ""), {})
    sig = inspect.signature(fn)
    for pname, param in sig.parameters.items():
        if param.kind in (param.VAR_KEYWORD, param.VAR_POSITIONAL):
            continue
        if pname in overrides:
            kwargs[pname] = overrides[pname]
            continue
        ok, val = _guess_arg(pname, str(param.annotation))
        if ok:
            kwargs[pname] = val
        elif param.default is param.empty:
            missing.append(pname)
    return kwargs, missing


def classify_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__} {exc}".lower()
    if any(m in text for m in KEY_MARKERS):
        return "KEY"
    return "FAIL"


def load_done() -> dict[str, dict]:
    done = {}
    if RESULTS.exists():
        for line in RESULTS.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                done[r["name"]] = r
    return done


def write_checklist(done: dict[str, dict], all_names: list[str]) -> None:
    order = ["PASS", "KEY", "FAIL", "TIMEOUT", "SKIP-ARGS", "PENDING"]
    rows = []
    counts = dict.fromkeys(order, 0)
    for n in all_names:
        r = done.get(n)
        status = r["status"] if r else "PENDING"
        counts[status] = counts.get(status, 0) + 1
        note = (r or {}).get("error", "") or (r or {}).get("note", "")
        secs = f"{r['seconds']:.0f}s" if r else ""
        rows.append(f"| {n} | {status} | {secs} | {note[:90]} |")
    CHECKLIST.parent.mkdir(parents=True, exist_ok=True)
    CHECKLIST.write_text(
        "# TRID3NT Local tool sweep -- direct-execution checklist\n\n"
        f"Updated: {dt.datetime.now().isoformat(timespec='seconds')}  \n"
        f"Total {len(all_names)} | " + " | ".join(f"{k} {v}" for k, v in counts.items() if v) + "\n\n"
        "AOI: ~3km downtown Tampa. KEY = needs an API key, earmarked for later.\n\n"
        "| tool | status | time | note |\n|---|---|---|---|\n"
        + "\n".join(rows) + "\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run a single tool by name")
    ap.add_argument("--limit", type=int, default=0, help="stop after N new results")
    ap.add_argument("--retry", action="store_true", help="re-run non-PASS tools too")
    args = ap.parse_args()

    import grace2_agent.tools as pkg
    from grace2_agent.tools import get_registered_tools
    for m in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"grace2_agent.tools.{m.name}")
    try:
        import grace2_agent.workflows as wpkg
        for m in pkgutil.iter_modules(wpkg.__path__):
            try:
                importlib.import_module(f"grace2_agent.workflows.{m.name}")
            except Exception:
                pass
    except ImportError:
        pass

    tools = {t.metadata.name: t for t in get_registered_tools()}
    prefetch_chain(tools)
    all_names = sorted(tools)
    done = load_done()
    todo = [n for n in all_names if args.only == n] if args.only else [
        n for n in all_names
        if n not in done or (args.retry and done[n]["status"] != "PASS")
    ]
    print(f"registry {len(all_names)} tools; {len(done)} done; {len(todo)} to run")

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    ran = 0
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    for name in todo:
        rt = tools[name]
        try:
            rt.fn.__tool_name__ = name
        except AttributeError:
            pass
        kwargs, missing = build_args(rt.fn)
        rec = {"name": name, "module": rt.module, "ts": dt.datetime.now().isoformat(timespec="seconds")}
        if missing:
            rec.update(status="SKIP-ARGS", seconds=0.0,
                       note=f"required params not fabricatable: {missing}")
        else:
            budget = _timeout_for(name)
            t0 = time.time()
            fut = pool.submit(
                (lambda f=rt.fn, kw=kwargs: __import__("asyncio").run(f(**kw))
                 if inspect.iscoroutinefunction(rt.fn)
                 else rt.fn(**kwargs))
            )
            try:
                result = fut.result(timeout=budget)
                rec.update(status="PASS", seconds=time.time() - t0,
                           result=str(result)[:120])
            except concurrent.futures.TimeoutError:
                rec.update(status="TIMEOUT", seconds=time.time() - t0,
                           error=f"exceeded {budget}s (thread abandoned)")
                # abandoned thread may still finish; pool replaced to stay serial
                pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            except BaseException as exc:  # noqa: BLE001 - classify everything
                rec.update(status=classify_error(exc), seconds=time.time() - t0,
                           error=f"{type(exc).__name__}: {exc}"[:200],
                           tb=traceback.format_exc()[-400:])
        with RESULTS.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        done[name] = rec
        print(f"[{len(done)}/{len(all_names)}] {name}: {rec['status']} "
              f"({rec.get('seconds', 0):.0f}s) {rec.get('error', '')[:80]}")
        write_checklist(done, all_names)
        ran += 1
        if args.limit and ran >= args.limit:
            break
    write_checklist(done, all_names)
    print("sweep pass complete")
    # A tool that blew its budget leaves an abandoned NON-DAEMON thread behind;
    # normal interpreter exit joins threads forever and the process zombies at
    # high CPU (seen live 2026-07-06: pass-1 spun 70+ min after finishing).
    # All results are already flushed to the JSONL, so exit hard.
    sys.stdout.flush()
    import os
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
