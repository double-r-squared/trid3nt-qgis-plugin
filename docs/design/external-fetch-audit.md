# External-fetch audit -- the network-reach inventory

Purpose: surface every path where TRID3NT reaches the public internet OUTSIDE the
one sanctioned fetcher substrate, and grade each against the visibility the
substrate provides. The charge (NATE, 2026-08-25): "look out for this violation
-- I don't think it's isolated." The trigger was the TELEMAC family: all five
worker builders fetch NOAA NGDC / nationalmap / USGS water / Planetary Computer
from inside their container, one of them through a private fallback ladder.

This is the sibling of `fallback-audit.md`. That audit asked *when we substitute,
is the swap declared?* This one asks the prior question: *when we fetch at all,
can anyone see it?* Their denominators differ, and that difference is the point --
the F-arc swept `trid3nt_server/` only, so no worker was ever in scope.

## The visibility contract

The universal fetcher router (`trid3nt_server/tools/fetchers/`) gives a fetch five
properties. Every row below is graded against these five, not against a vague
sense of "hidden":

1. **EMIT** -- a renderable result is auto-surfaced as an `Input: ...` context
   layer wherever it is fetched (ADR 0244 `maybe_emit_input_on_fetch`). The user
   sees the data the model ran on.
2. **LADDER** -- a cross-dataset alternative is a DECLARED rung walked by the
   shared walker, with an activation row and a coverage share
   (`trid3nt_server/fallbacks/`, ADRs 0289-0293/0299).
3. **CACHE** -- read-through with a provenance sidecar (`data/cache.py`), so the
   same bytes are not re-pulled and the pull is recorded.
4. **PROVENANCE** -- the sidecar plus the spec card the model reads: what source,
   what endpoint, what resolution.
5. **RETRY DOCTRINE** -- mirrors, backoff, and the upstream-vs-internal error
   split, ending in an honest typed error rather than a raw exception.

Two structural greps fix the frame for the whole table:

- `maybe_emit_input_on_fetch` / `emit_on_fetch` -- **zero callers outside
  `trid3nt_server/tools/fetchers/`**.
- `record_provenance` -- **zero callers outside `data/fetchers/` and
  `data/cache.py`**.
- `get_ladder` / `walk_ladder` -- exactly one production caller,
  `_router/router.py:757-768`, keyed on `spec.name`.

So properties 1, 2 and 4 are structurally unavailable to every row below. There
is no site at which a bypass could accidentally have been visible.

## Method + denominator

Swept for: `requests` / `httpx` / `urllib.request` / `urllib3` / `aiohttp` /
`http.client` / `ftplib` / `socket`; `pystac` / `pystac_client` /
`planetary_computer` / `pooch` / `owslib` / `earthaccess` / `cdsapi` /
`dataretrieval` / `siphon`; `boto3` / `botocore` / `s3fs` / `fsspec` against
non-TRID3NT buckets; GDAL `/vsicurl/` `/vsis3/` `/vsiaz/` `/vsigs/` on external
hosts; `rasterio.open` / `xarray.open_*` on `http(s)://`; subprocess
`curl`/`wget`/`aria2c`/`git clone`; and -- because a literal-URL grep misses a
built URL -- host-ish constants (`_BASE_URL`, `_ENDPOINT`, `_HOST`, `exportImage`,
`arcgis/rest`, `ImageServer`, `MapServer`, `stac`, `catalog.json`, `urljoin`).

Denominator, stated:

| Tree | Files | In scope | Result |
|---|---|---|---|
| `workers/` | 107 .py (non-test) + 14 Dockerfiles across 23 dirs | all | 8 fetch sites in our own worker code, ALL in `workers/telemac/`; 2 more inside solver libraries (sfincs/hydromt, openquake engine) |
| `trid3nt_server/` outside `data/fetchers/` | 595 .py | all | 14 distinct egress paths |
| `trid3nt_server/tools/fetchers/` | 211 .py | excluded | the substrate itself |
| `scripts/sandbox/` + every bind-mounted host dir | 45 .py | all | 1 promoted-to-product module, rest dev-only |
| `plugin/` | 73 .py | all | clean beyond daemon + basemaps |
| `contracts/` | 65 .py | all | clean |
| `scripts/` (non-sandbox) | 151 .py | all | 7 carry a network call, all dev-driver |

Source grepping alone is NOT the method, and the two most surprising rows prove
it. Where a worker hands control to a solver library, the library's own source
was traced in the INSTALLED copy (`hydromt` 0.10.1 and `openquake.engine` 3.20.1
under `~/.cache/uv/archive-v0/`) down to the line that opens the socket. And
three live images were inspected read-only -- `pip list`, an import probe, and
`command -v curl wget` -- because a Dockerfile's explicit `pip install` lines are
not evidence about what is actually baked.

Audited against working tree `aa844d28` (the kickoff cited `76a65945`; the only
commit between them adds proof PNGs and one proof script -- no code under audit
changed). Live image inspection ran `pip list` and `command -v curl wget` inside
`trid3nt-local/{telemac,sfincs,geoclaw}:latest` read-only.

Classifications used. Three beyond the kickoff's five, because the evidence
demanded them: **BUILD-TIME** (Dockerfile-only, no runtime reach),
**CAPABILITY-ONLY** (the image can fetch, no code does), and **DEV-DRIVER**
(hand-run tooling with no product call site -- proven by an absent caller, not
assumed).

## A. In-worker fetch -- the TELEMAC family

The escalated class. Every row: fires inside the solver container, over the
default docker bridge network, with no `--network` restriction
(`workflows/telemac/run_telemac.py:182-194` and every sibling `build_argv`).
The container is trusted with the public internet but is NOT given object-store
credentials -- inputs and outputs move through the `-v <rundir>:/data` mount.

| # | WHERE | HOST / DATASET | WHEN | EMIT / LADDER / CACHE / PROV / RETRY | CLASS + VERDICT |
|---|---|---|---|---|---|
| 1 | `workers/telemac/telemac_river_dye_build.py:375-383` `_snap_comid`, `:644-645` NLDI navigation | `api.water.usgs.gov/nldi/linked-data` -- NHDPlus flowline navigation. **This is the model centerline**: the mesh, the reach, the domain | EVERY `telemac_river_dye` and `telemac_do_sag` solve | none / none / none / none / none | **IN-WORKER-FETCH -- WORST ROW.** See the false-surface note below |
| 2 | same file `:391-418` `_named_flowline_seed`, `:~460-474` `_mainstem_flowline_seed` | `hydro.nationalmap.gov/.../NHDPlus_HR/MapServer/3/query` -- GNIS-named flowline re-seed | every solve where `river_name` is set (named) or absent (mainstem) | none / none / none / none / fail-open to the raw seed | IN-WORKER-FETCH |
| 3 | same file `:845-850` `fetch_bank_polygons` | `hydro.nationalmap.gov/.../NHDPlus_HR/MapServer/8/query` -- NHDArea water polygons (the real bank geometry) | only on `bank_source="nhd_area"` | none / none / none / none / single attempt; failure raises `BanksUnavailableError` -> the retryable `TELEMAC_BANKS_UNAVAILABLE` gate | IN-WORKER-FETCH. The GATE is gold (fallback-audit row 23); the FETCH is still invisible |
| 4 | same file `:1547-1554` knobs, `:1576-1603` `_sample_dem_stac`, `:1606-1646` `_sample_dem_3dep`, `:1649-1695` `_fetch_dem_samples` | `planetarycomputer.microsoft.com/api/stac/v1` -> signed Azure COGs via `/vsicurl/`, falling to `elevation.nationalmap.gov/.../3DEPElevation/ImageServer/exportImage` | every river solve | bed-COG NAME LABEL only / **private** / none / none / 3x STAC with 5/20/60 s backoff, then 3DEP, then a plain `RuntimeError` | **PRIVATE-LADDER.** A CROSS-DATASET swap (Copernicus GLO-30 radar -> USGS 3DEP lidar) -- exactly what fallback-audit row 1 made user-gated on the agent side -- running silently in a container |
| 5 | `workers/telemac/telemac_coastal_build.py:52-54`, `:182-204` `fetch_demall_bed`, called `:306` | `gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_all/ImageServer/exportImage` | `bathy_source` defaults to `"noaa_demall"` (`:83`), so every coastal solve that is not explicitly `synthetic` | bed-COG name label / none / none / none / **none** | IN-WORKER-FETCH |
| 6 | `workers/telemac/telemac3d_build.py:77-79`, `:396-418`, called `:469` | same NOAA DEM_all mosaic | UNCONDITIONAL, every `telemac3d` solve | **NOTHING** -- telemac3d is the one builder that writes no bed COG / none / none / none / **none** | IN-WORKER-FETCH -- **zero visibility of any kind** |
| 7 | `workers/telemac/tomawac_build.py:54-56`, `:373-399`, called `:453` | same NOAA DEM_all mosaic | UNCONDITIONAL, every `tomawac` solve | bed-COG name label / none / none / none / **none** | IN-WORKER-FETCH. **This row killed the tomawac showcase** |
| 8 | `workers/telemac/artemis_build.py:69-71`, `:399-422`, called `:557` | same NOAA DEM_all mosaic | UNCONDITIONAL, every `artemis` solve | bed-COG name label / none / none / none / **none** | IN-WORKER-FETCH |

Rows 5-8 are the same twenty lines copy-pasted four times. All four are
single-shot: `timeout=180`, no retry, no backoff, no mirror. `resp.raise_for_status()`
raises `requests.exceptions.HTTPError`, which `entrypoint.py:1433-1441` writes out
as `{"status":"error","error":"HTTPError: 500 Server Error ..."}` with **no
`error_code`** -- untyped, non-retryable, un-actionable. The router's own doctrine
for the identical failure (mirror, backoff, `upstream_provider` error class,
honest typed error) is thirty metres away and unreachable from inside the image.

**Row 1's false surface.** `telemac_river_dye`'s plan declares
`Data("rivers", Fetch.tool(".../reach.fetch_reach_flowline"))`
(`river_dye/river_dye.py:268`). That producer routes `fetch_river_geometry`,
whose spec is **OSM Overpass** (`data/fetchers/hydrology/fetch_river_geometry/
source.yaml:7-13`), and it is emitted to the canvas as
`Input: river geometry`. Its ONLY consumer is `reach_seed`
(`steps/reach.py:434-448`), which reduces the whole layer to a single mid-reach
lon/lat. The worker then throws that away and re-fetches its own centerline from
NLDI/NHDPlus. So the river the user sees on the canvas is a different dataset,
from a different provider, than the river the mesh was built on. ADR 0231's
disposition table records "telemac river_dye/do_sag river geometry | SURFACED
(audit wave)". That row is wrong in the same way fallback-audit rows 8, 9 and 27
were wrong when written: the surfaced artifact is not the consumed one.

**The doctrine that covers rows 4-8, and its ceiling.** ADR 0231:177 rules that
"any future in-worker fetch surfaces the same way (worker writes COG + records
key -> composer emits via the existing raster-input seam)", and ADR 0308 added a
HEAD existence check so a dead COG is skipped rather than published as a 404
layer. That is a real, working seam -- it is why four of these five builders put
a bed layer on the canvas. But it delivers property 1 ALONE, and only for data
with a raster form. It gives no ladder, no cache, no provenance sidecar, no
coverage claim, and no retry doctrine. Rows 1, 2 and 3 -- vector geometry that
IS the domain -- get nothing at all from it.

**The staging seam already exists and is deliberately empty.**
`workflows/telemac/steps/deck.py:93-98` writes the worker manifest with:

```
"inputs": [],        # the pipeline self-fetches NHDPlus + the DEM
```

`data/simulation/solver/solver.py:978-1000` is a generic, engine-agnostic loop
that downloads every `{"gs_uri": ..., "dest": ...}` entry into the rundir before
launch, path-traversal-guarded. SFINCS uses it (`dem_uri` from a router fetch
reaches the build spec). TELEMAC passes an empty list and a comment naming the
bypass. The migration in section 2 below is not new machinery; it is filling in
a list.

## B. Other workers -- and the class nobody was looking for

NO ENGINE OUTSIDE `workers/telemac/` FETCHES IN ITS OWN CODE. Every non-telemac
URL literal in `workers/` is build-time or inert, proven in section F rather than
assumed, and 25 worker directories came back clean against all six pattern
classes plus a catch-all hostname regex.

Two engines fetch anyway -- **through their solver library**, on every solve, in
code we do not own and no grep of our source would ever surface. This is a class
the kickoff did not anticipate and it is the most structurally important finding
in the audit: a source lint cannot see it, and the only control that reaches it
is network posture.

| # | WHERE | HOST / DATASET | WHEN | EMIT / LADDER / CACHE / PROV / RETRY | CLASS + VERDICT |
|---|---|---|---|---|---|
| 23 | `workers/_sfincs_build/deck.py:2526-2527` `SfincsModel(...).build(...)` -> `hydromt/data_catalog.py:75,147-149` -> `hydromt/predefined_catalog.py:18,109-113` | `raw.githubusercontent.com/Deltares/hydromt/main/data/catalogs` via **pooch** | EVERY regular-grid SFINCS build-mode solve (`workers/sfincs/entrypoint.py:644`), on the first `data_catalog.get_*` call. NOT the quadtree branch (`:643`, cht_sfincs, never touches hydromt) | none / none / pooch's `~/.hydromt_data`, **not pre-warmed in the image** / none / pooch `retry_if_failed=3` | **IN-WORKER-FETCH (library-originated).** GitHub availability is an undeclared dependency of every SFINCS build |
| 24 | `workers/openquake/entrypoint.py:262` `oq engine --run` -> `openquake/engine/engine.py:269` `check_obsolete_version` -> `:463-465` | `api.openquake.org/engine/latest` | EVERY OpenQuake solve. Suppressed only by `JENKINS_URL` or `CI` in the env (`engine.py:453`); **neither is set** in `workers/openquake/Dockerfile:105-116` or the dispatch env | none / none / none / none / one attempt, `timeout=1`, swallowed by a bare `except Exception` -> `logging.warning` into `oq.stderr` | **OUTBOUND TELEMETRY -- KILLED 2026-08-24.** The `User-Agent` (`:457-459`) carried engine version, `calculation_mode`, `platform.platform()` and `oq_distribute`. `CI=1` now set in **both** places: `env_overrides` in `trid3nt_server/workflows/openquake/psha/psha.py` (`openquake_local_spec()`) -- the LIVE dispatch, since `exec_kind="exec"` runs `oq` as a host subprocess (`run_oq.py`), never `workers/openquake/Dockerfile`'s image; and `ENV CI=1` added to the Dockerfile itself for the dormant AWS-Batch lane. Evidence: direct instrumentation of `openquake.engine.engine.urlopen` shows the call fires (and reaches the real `api.openquake.org`, returning a real "newer version available" payload) with `CI` unset, and is never invoked with `CI=1` set -- plus the live local-exec smoke (`scripts/run_openquake_direct.py`, status=ok) whose `oq.stderr` carries no `Using engine version` line, the log statement `check_obsolete_version` only reaches after the CI short-circuit |

Row 23 corrects a wrong reading made earlier in this audit. The first pass
concluded CAPABILITY-ONLY on the evidence that our code never constructs a
`DataCatalog` and never names `deltares_data` / `artifact_data` -- both true, and
both irrelevant. `hydromt`'s `DataCatalog.__init__` defaults
`fallback_lib="artifact_data"`, and its `sources` property fetches the predefined
catalog whenever `_sources` is empty. `get_rasterdataset` (`data_catalog.py:1260`)
touches `.sources` BEFORE deciding the argument is a plain local path, so the
fetch fires on the first call -- and `setup_dep`, which our YAML always emits
(`_sfincs_build/deck.py:1906`), is that first call. `SfincsModel` exposes no way
to pass `fallback_lib=None`, so the practical fixes are baking the catalog into
`~/.hydromt_data` at image build or clearing `model.data_catalog._fallback_lib`
immediately after construction. **A dependency's default is our behaviour.**

Row 24 is the only outbound telemetry in the system, and the leak is not just a
version ping: `calculation_mode` tells a third party what kind of hazard
calculation a user is running. One env var (`CI=1`) closes it.

Everything else, and the capability postures the guard has to hold down:

| Image | Baked fetch capability (live `pip list` where an image exists) | Runtime fetch code | Verdict |
|---|---|---|---|
| telemac | `requests 2.34.2`, `pystac-client 0.9.0`, `planetary-computer 1.0.0`, `pystac 1.15.2`, `rasterio 1.3.11`. No curl/wget | rows 1-8 | IN-WORKER-FETCH (section A) |
| sfincs | `requests 2.34.2`, `pooch 1.9.0`, `pystac 1.15.2`, `fsspec`, `rasterio 1.4.4`. No curl/wget | row 23, via hydromt | IN-WORKER-FETCH. The Dockerfile never names `requests` or `pooch` -- both arrive transitively with `hydromt`, and `pooch` is exactly what row 23 fetches with. Static Dockerfile review called this image clean; the live `pip list` is what caught it |
| geoclaw | `boto3`, `rasterio 1.4.4`. No requests, no pystac. **`curl` present at `/usr/bin/curl`** (kept deliberately, per the Dockerfile's own comment) | none. The generated `maketopo.py` imports only numpy + `clawpack.geoclaw.dtopotools`; no `get_remote_file`, no clawpack remote-topo target | CAPABILITY-ONLY (shell-out reach) |
| swan | boto3, rasterio, scipy. `curl` kept in the runtime stage | none. The `fetch_topobathy` strings at `entrypoint.py:203,317,348,416` are ERROR PROSE naming an agent-side ladder; `_assert_bottom_has_wet_cells` raises `SWAN_ALL_DRY_GRID` rather than substituting | CAPABILITY-ONLY |
| elmfire | boto3 + `gdal-bin` CLI in the runtime stage (`/vsicurl`-capable; `elmfire_io.f90` does shell out to `gdal_translate`) | none. `deck_builder.py:373-385` takes the bucket off the `s3://` URI the server put in `build_spec["inputs"]` -- no host constant. `:390-394` HARD-REJECTS any other scheme, asserted by `tests/test_deck_builder.py:428-432` | **CAPABILITY-ONLY, and the model to copy.** The only worker with a scheme allow-list |
| modflow, landlab, schism, mesh, hecras, hecras2025, qgis | boto3/rasterio (all but schism, hecras2025, qgis); no requests/pystac/pooch declared; curl/wget confined to discarded build stages. `mesh/coastal_tin_build.py:59,73` passes LOCAL paths into `om.Shoreline` / `om.DEM` -- oceanmesh never resolves a coastline itself; the GSHHS shapefile arrives on the bind mount. `workers/qgis/` contains no Python at all | none | CLEAN |
| canopy | boto3, rasterio, `geoai-py`, torch CPU wheels | none in our code (`entrypoint.py` has zero http/requests/urllib; `_download` is s3/gs scheme-dispatched). Weights are baked into `/opt/trid3nt/weights/canopy` and handed to the estimator at `:336-339` | BUILD-TIME, with **one open caveat**: `entrypoint.py:327` imports `geoai.canopy.CanopyHeightEstimation`, whose `cache_dir=` semantics are download-if-absent. If the baked filenames do not match what geoai looks for, a 749 MB pull happens at SOLVE time from the same public bucket. The Dockerfile itself flags the filenames as unconfirmed (`:89-91`, "GATED-BUILD PLACEHOLDER"), `geoai` is not installed on this box, and canopy IS wired live (`data/processing/compute_canopy_height:72` dispatches `run_solver('canopy')`). **Unresolved -- do not record as clean** |

**One latent door, queued not tabled.** `_sfincs_build/deck.py:2422` `_localize()`
has no scheme allow-list: `if not (uri.startswith("s3://") or uri.startswith("gs://")):
return uri  # already a local path`. An `https://` DEM URI arriving in a
build_spec would pass `_stage_gcs_local` untouched (it rejects only s3/gs) and
land in `rasterio.open()` at `:883` / `:1195`, where GDAL's HTTP driver fetches
it. Server-originated rather than worker-originated, so it is not a fetch row --
but it is an unguarded door, and elmfire's `if "://" in path_or_uri: raise` is the
one-line pattern to copy. Everything else in `_sfincs_build` is strict: the
`/vsis3/` converter at `:676-706` is dead for the rasterio path because
`_stage_gcs_local` (`:2074-2090`) is identity-or-raise, and `deck_quadtree.py:106-110`
explicitly stubs `cht_bathymetry.bathymetry_database` to `None` to block
cht_sfincs's own external bathymetry DB.

**Out of scope, named so the reader is not surprised by its absence.**
`geoclaw/setrun_builder.py:1077-1101` and `openquake/job_ini.py:396,1003` both
carry in-worker MODEL degrades (finite-fault -> single rectangular subfault;
fault source -> area source). Neither rung reaches the network -- both are fed by
staged data -- so they are fallback-audit business, not fetch-visibility business.
Both are banner-flagged in their generated decks.

BUILD-TIME fetches (Dockerfile only, no runtime reach, all SHA256-pinned unless
noted): `hec.usace.army.mil` (hecras 6.x zip), `github.com` releases (modflow6,
gridgen, clawpack, elmfire, hec-ras 2025, oceanmesh via `pip install git+`,
micromamba), `swanmodel.sourceforge.io`, `schism-dev/schism` git clone,
`download.pytorch.org`, and `dataforgood-fb-data.s3.amazonaws.com` for the canopy
model weights -- **the only unpinned one**, flagged as a gated placeholder in the
Dockerfile itself. Supply-chain note, not an egress finding.

Also noted, not a fetch finding: a stale
`226996537797.dkr.ecr.us-west-2.amazonaws.com/grace2-elmfire:latest` image is
still on this box, post-decommission.

## C. Server bypasses -- fetching outside the router

Fourteen distinct paths. None emits, none writes a provenance sidecar. Three
carry a mirror ladder, all three private and mutually divergent; the rest are
single-attempt.

| # | WHERE | HOST / DATASET | WHEN | VISIBILITY | CLASS |
|---|---|---|---|---|---|
| 9 | `workflows/mesh/generate_mesh/generate_mesh.py:352-366` -> `scripts/sandbox/oceanmesh/water_edge.py:80-106` and `:325-383` | Overpass (`overpass-api.de`, `overpass.kumi.systems`, `overpass.private.coffee`) for `natural=coastline`; `hydro.nationalmap.gov` NHDPlus_HR for NHDArea/NHDWaterbody | every coastal `generate_mesh`. `_infer_mode:190-201` returns `coastal_water_edge` as the **unconditional fallthrough default** -- any call without a pour point lands here | private 3-mirror ladder + 3x backoff + UA; loud NHD degrade log. No emit, cache, sidecar | **SERVER-BYPASS -- WORST ROW.** A `scripts/sandbox/` module `sys.path`-injected into the LIVE DAEMON (alongside `workers/schism`) and run in-process via `asyncio.to_thread`. Contradicts ADR 0112 ("coded data-fetchers = 0, one router") on a hot default path |
| 10 | `workflows/geoclaw/finite_fault.py:53,277`, reached from `inundation.py:474` | `earthquake.usgs.gov/fdsnws/event/1/query` + the finite-fault `.fsp` product | every GeoClaw solve on a real event | single attempt, typed `FINITE_FAULT_FSP_FETCH_FAILED`. No emit/ladder/cache/sidecar | SERVER-BYPASS. The fetched object IS the earthquake source model |
| 11 | `workflows/geoclaw/scenario_slab2.py:88,299`, reached from `inundation.py:564` | `sciencebase.gov/catalog/items` + per-zone Slab2 `.grd` downloads | every GeoClaw scenario solve | single attempt, typed error. No emit/ladder/cache/sidecar | SERVER-BYPASS. Slab geometry drives the rupture |
| 12 | `workflows/telemac/agitation/agitation.py:403-429` | Overpass `man_made=breakwater` via `overpass-api.de`, `overpass.kumi.systems`, **`maps.mail.ru/osm/tools/overpass`** | every `artemis_harbor_agitation` solve | private 3-mirror ladder (a DIFFERENT third mirror from rows 9 and the router spec), best-effort `[]` -> labeled schematic barrier | SERVER-BYPASS + PRIVATE-LADDER |
| 13 | `workflows/schism/baroclinic_circulation/baroclinic_circulation.py:336-372`, called `:544` | `hydro.nationalmap.gov/.../NHDPlus_HR/MapServer/8/query` -- NHDArea | every `schism_baroclinic_circulation` solve | single attempt, best-effort `None` -> full-rectangle mesh with a loud note | SERVER-BYPASS. A third independent raw client for the same NHDArea endpoint as rows 3 and 9 |
| 14 | `mesh/swmm_deck_runner.py:142-179, 322`, reached from `workflows/swmm/deck_runner/deck_runner.py:187` | `openswmm.org/Topic/{15609,14400,10082,...}` -- forum pages scraped for the published `.inp` deck | every SWMM deck-runner solve | single attempt, typed `SWMM_DECK_UNAVAILABLE`, **deliberately un-cached** for redistribution honesty (`:133`) | SERVER-BYPASS. The fetched deck IS the model; availability of a forum thread is a physics dependency |
| 15 | `workflows/swmm/network_import/network_import.py:271,307` | **arbitrary caller-supplied `https://`**, plus any keyless ArcGIS FeatureServer/MapServer, paginated | every `swmm_network_import` with an http(s) source | single attempt, no ladder, no cache/sidecar | SERVER-BYPASS (user/model-directed). The host is whatever lands in `source` |
| 16 | `data/processing/compute_building_density/compute_building_density.py:271,339` | `minedbuildings.z5.web.core.windows.net` -- MS Global ML Building Footprints CSV index (~7 MB) + `.csv.gz` tiles | every call | process-lifetime `_INDEX_CACHE`; output through `read_through`. No fetch-time cache, ladder, sidecar or emit | SERVER-BYPASS |
| 17 | `data/fetchers/imagery/_pc_stac.py:43,48,110-145`, consumed by `data/processing/{compute_ndvi:309,digitize_water_body:358,compute_change_detection:364}` | `planetarycomputer.microsoft.com/api/stac/v1` + `/api/sas/v1/token`, then Azure COGs via `/vsicurl/` | every NDVI / water-digitize / change-detection call | 45-min SAS token cache; output `read_through`. Uses raw `requests` + `pystac_client`, **not** `_router/transport/` | SERVER-BYPASS. It lives under `fetchers/` but sits outside the router's transport, so it inherits none of the five properties |
| 18 | `data/search/ogc_adapter.py:51,455` | arbitrary OGC endpoint (WMS/WCS/WFS/ArcGIS REST) via raw `requests.get` | the shared substrate under `fetch_from_catalog` Tier-2 | single attempt, no cache/sidecar/emit | SERVER-BYPASS (discovery-shaped; see the note below) |
| 19 | `data/search/fetch_from_catalog/fetch_from_catalog.py:268` | arbitrary catalog-entry URL, Tier-3 | every Tier-3 call | single attempt | SERVER-BYPASS (discovery-shaped) |
| 20 | `data/search/web_fetch/web_fetch.py:60,257` | any URL the model supplies, via `httpx` | every `web_fetch` call | typed upstream/input errors, read-through shim | **BY DESIGN.** The widest single egress surface in the server, but it is the declared open-web tool, not an accident. Named for completeness |
| 21 | `server/protocol/catalog_http.py:1338-1360`, dispatched at `:2186` | `overpass-api.de/api/interpreter` | `GET /api/building-detail` on the daemon HTTP port, when the S3 tag-sidecar scan misses | single attempt; swallowed to a typed 404 | **SERVER-BYPASS + ORPHAN.** Repo-wide grep for `building-detail` finds no caller in `plugin/`, `contracts/` or tests. Web-era leftover; deletion-ledger candidate |
| 22 | `data/display/show_nexrad_radar/show_nexrad_radar.py:61` | `mesonet.agron.iastate.edu/cgi-bin/wms/nexrad` | every call | `cacheable=False`, `ttl_class="live-no-cache"` | **Not a server fetch** -- the daemon composes a WMS GetMap URL and QGIS reaches Iowa State directly. Named because a third-party host enters the CLIENT's egress set through a server tool, unmediated on both sides |

Rows 18-20 are discovery-shaped: their whole job is to reach a host the model
named, so "route it" is a weaker prescription than for rows 9-17. They still owe
the retry doctrine and a provenance record of what was actually pulled.

**Four divergent Overpass mirror lists** now exist for one service: the router
spec (`overpass-api.de`, `kumi`, `private.coffee`), `water_edge.py` (same three),
`agitation.py` (`overpass-api.de`, `kumi`, `maps.mail.ru`), and `catalog_http.py`
(single, no mirror). A change to Overpass etiquette has to be made in four
places, and one of them routes traffic through a Russian-hosted mirror that no
other path uses.

## D. Bind mounts and in-container scripts

Every host directory bind-mounted into a solver container by product code:

| Host dir | Container path | Product call site |
|---|---|---|
| `scripts/sandbox/oceanmesh` | `/sandbox` | `workflows/telemac/rain_on_grid/mesh_acquisition.py:313-314`; `workflows/mesh/generate_mesh/generate_mesh.py:652` |
| per-run `<rundir>` (ephemeral) | `/data` | `data/simulation/solver/solver.py:625`; `run_hecras.py:166`; `run_schism.py:159`; `run_telemac.py:188/280/373/467/562`; `data/meta/passthroughs.py:187`; `mesh/coastal_tin.py:114` |
| per-run `<rundir>` (ephemeral) | `/deck` | `workflows/elmfire/run_elmfire.py:839` |
| `$TRID3NT_GSHHG_SHP` parent | `/shoreline` | `workflows/schism/tidal_hydro/tidal_hydro.py:733` -- data file, no code |

`scripts/sandbox/oceanmesh` is the only persistent host CODE tree mounted into a
container. Both of its in-container entrypoints are CLEAN: no network import, no
URL literal, pure geometry/oceanmesh/rasterio/scipy
(`_mesh_watershed_incontainer.py`, `_mesh_water_edge_incontainer.py`).

So there are **no IN-CONTAINER-FETCH findings outside `workers/telemac/`**. The
fetching in that directory (`water_edge.py`) was pulled OUT of the container and
into the daemon -- row 9.

**The `build_coastal_mesh.py:91` lead, resolved against its premise.** The IDEAS
note recorded it as carrying "the pre-F2 `fetch_dem`-shaped bed helper the product
path deleted in ADR 0299". It does not. Its `_select_and_merge(...)` call matches
the live signature at `_router/hooks/topobathy.py:1264-1274` argument for
argument -- it reuses the current sanctioned hook. It is `__main__`-guarded
(`:384-385`), self-declared standalone, and has zero references from
`trid3nt_server/`. **DEV-DRIVER.** The note should be retired; a correct stop
beats a wrong execution.

**The `code_exec` playground is not an egress path** -- verified, not assumed.
`trid3nt_server/sandbox/` runs under bwrap with `--unshare-net`
(`sandbox_hardening.py:33`), plus an in-process `socket.connect` /
`connect_ex` / `create_connection` monkeypatch and proxy-env stripping
(`sandbox_executor.py:178-230`), and fails CLOSED when bwrap is missing
(`:368-369`). Hygiene note only: `DEFAULT_NET_ALLOW` (`:102-107`) still lists
`googleapis.com`, `google.internal`, `mongodb.net` -- dead GCP/Atlas-era hosts,
moot behind the netns but stale.

**DEV-DRIVER, each proven by an absent product call site** (basename grep across
`trid3nt_server/` returns nothing, or only provenance prose):
`scripts/sandbox/oceanmesh/merc_render.py:19,25,87` (ArcGIS World_Imagery tiles,
serving the proof-render basemap norm); `water_edge.py:454` `_probe_cusp`
(`gis.charttools.noaa.gov`, reachable only from that file's `__main__` -- dev-only
code inside an otherwise product-live file); `scripts/harvest_living_atlas.py`;
`scripts/stage_groundwater_recharge.py`; `scripts/stage_zell_sanford_groundwater.py`;
`scripts/proof_hecras_equation_stability.py`;
`scripts/sandbox/replication/edi_coweeta_coverage.py`.

## E. Plugin and contracts

Nothing beyond the two expected channels. Noted, not deep-dived, per the kickoff.

- LEGITIMATE: `plugin/net/trid3nt_client.py`, `plugin/render/probe.py`,
  `plugin/case/push_layer.py` reach only the daemon (`ws://127.0.0.1:8765`,
  `http://127.0.0.1:8766`) and MinIO (`:9000`), user-editable to a tailnet peer.
- LEGITIMATE: `plugin/render/layers.py:126,137,143` XYZ basemaps
  (`tile.openstreetmap.org`, `server.arcgisonline.com`, `basemaps.cartocdn.com`)
  on explicit preset selection.
- No telemetry (zero sentry/bugsnag/mixpanel/amplitude/posthog hits; "telemetry"
  in this repo means solve-progress WS ticks). No update check -- plugin
  self-update was REMOVED 2026-07 (`plugin/ui/settings_dialog.py:266-267`); the
  daemon self-hosts `plugin_repo.py` locally. No plugin-side dataset fetching:
  zero `pystac`/`owslib`/`planetary_computer`/`pooch`/`fsspec`/`s3fs` imports,
  zero WFS/WMTS/OAPIF/STAC provider strings, zero Qt network classes -- all
  plugin HTTP is stdlib `urllib` to the daemon.
- INERT, call-site traced: `plugin/ui/settings_dialog.py:41-91` `PROVIDER_PRESETS`
  holds `openrouter.ai` / `api.openai.com` / `api.groq.com`, but both consumers
  (`:347`, `:376`) resolve through `_resolve_http_base()` and hit the DAEMON's
  `/api/provider-config` and `/api/local-models`. The plugin never opens a socket
  to a provider.
- INERT: `contracts/trid3nt_contracts/chart_contracts.py:99` vega schema URL is
  docstring-only; the validator checks for the `$schema` KEY and never
  dereferences it. All `contracts/tests/` URLs are Pydantic fixture values and no
  file in `contracts/` imports a network library.
- DEV-DRIVER: `plugin/install_dependencies.py` builds a pip command string that
  `plugin/ui/charts_window.py` DISPLAYS for the user to copy. "The panel never
  runs anything itself."

## F. Proven INERT

Inertness is proven here, not assumed -- the kickoff's standard.

- `workers/openquake/job_ini.py:157-158, 404-405, 430-431, 455-456, 529-530,
  556-557, 649-650, 730-731` -- all 16 `www.opengis.net` / `openquake.org` hits
  are `xmlns:gml=` / `xmlns=` attributes WRITTEN INTO generated NRML documents.
  They are XML namespace identifiers, never dereferenced; no schema validation is
  enabled anywhere in the worker. Proven on both sides: the WRITE side imports
  only `math` / `dataclasses` / `typing` (`:33-37`) -- nothing in that module can
  dereference anything -- and the READ side, `openquake/hazardlib/nrml.py:89-93`,
  holds them as prefix-mapping constants for a `ValidatingXmlParser` built on
  `xml.parsers.expat` (`baselib/node.py:155`), with no `XMLSchema`, DTD or
  `resolve_entities` anywhere in either file. Expat does not resolve namespace
  URIs. NATE's IDEAS note guessed "doc headers" -- close, and the verdict is the
  same: **INERT**. (This is a separate question from row 24: `job_ini.py` is
  inert, the ENGINE that consumes its output is not.)
- `workers/elmfire/deck_builder.py` `elmfire.io` -- documentation citation in a
  comment. The lazy boto3 at `:373-385` downloads an `s3://` URI named in the deck
  spec, i.e. our own store. INERT + LEGITIMATE.
- `workers/hecras2025/subst/crux/transplant/REPRODUCE_TRANSPLANT.txt` and
  `subst/build{2,3}/*.sh` -- reproduction notes and build scripts, not runtime.
  BUILD-TIME.
- `workers/elmfire/tests/test_deck_builder.py` `example.com`,
  `plugin/tests/*` `firms.modaps.eosdis.nasa.gov` (a `signup_url` STRING rendered
  in a credential card) and `vega.github.io` (`$schema` fixture values) -- test
  fixtures. INERT.

## Summary by class

Counts per class per engine/tree. Rows 1-22 are the enumerated findings;
capability-only and build-time observations are counted separately because they
are postures, not fetches.

| Tree / engine | IN-WORKER-FETCH | PRIVATE-LADDER | SERVER-BYPASS | CAPABILITY-ONLY | BUILD-TIME | DEV-DRIVER | INERT |
|---|---|---|---|---|---|---|---|
| workers/telemac (river_dye + do_sag) | 3 (rows 1,2,3) | 1 (row 4) | -- | -- | -- | 1 (`rainfall_forcing_compare.py`) | -- |
| workers/telemac (coastal) | 1 (row 5) | -- | -- | -- | -- | -- | -- |
| workers/telemac (telemac3d) | 1 (row 6) | -- | -- | -- | -- | -- | -- |
| workers/telemac (tomawac) | 1 (row 7) | -- | -- | -- | -- | -- | -- |
| workers/telemac (artemis) | 1 (row 8) | -- | -- | -- | -- | -- | -- |
| workers/sfincs + _sfincs_build | **1 (row 23, library)** | 0 | -- | -- | 1 | -- | -- |
| workers/openquake + postprocess | **1 (row 24, library, telemetry)** | 0 | -- | -- | 1 | -- | 16 hits / 1 site |
| workers/geoclaw + postprocess | 0 | 0 | -- | 1 (curl in runtime) | 1 | -- | -- |
| workers/swan + postprocess | 0 | 0 | -- | 1 (curl in runtime) | 1 | -- | -- |
| workers/elmfire | 0 | 0 | -- | 1 (gdal-bin CLI) | 1 | -- | 1 |
| workers/modflow (+build, postprocess) | 0 | 0 | -- | 0 | 1 | -- | -- |
| workers/landlab + postprocess | 0 | 0 | -- | 0 | 0 | -- | -- |
| workers/schism | 0 | 0 | -- | 0 | 1 | -- | -- |
| workers/mesh | 0 | 0 | -- | 0 | 1 | -- | -- |
| workers/hecras + hecras2025 | 0 | 0 | -- | 0 | 2 | -- | 2 |
| workers/canopy | 0 (1 UNRESOLVED) | 0 | -- | 1 | 1 (unpinned) | -- | -- |
| workers/qgis, _raster_postprocess | 0 | 0 | -- | 0 | 0 | -- | -- |
| server: mesh/generate_mesh | -- | -- | 1 (row 9) | -- | -- | -- | -- |
| server: workflows/geoclaw | -- | -- | 2 (rows 10,11) | -- | -- | -- | -- |
| server: workflows/telemac | -- | 1 (row 12) | 1 (row 12) | -- | -- | -- | -- |
| server: workflows/schism | -- | -- | 1 (row 13) | -- | -- | -- | -- |
| server: mesh + workflows/swmm | -- | -- | 2 (rows 14,15) | -- | -- | -- | -- |
| server: data/processing + imagery | -- | -- | 2 (rows 16,17) | -- | -- | -- | -- |
| server: data/search | -- | -- | 3 (rows 18,19,20) | -- | -- | -- | -- |
| server: server/protocol | -- | -- | 1 (row 21, orphan) | -- | -- | -- | -- |
| server: data/display | -- | -- | 1 (row 22, client-side) | -- | -- | -- | -- |
| scripts/sandbox + scripts | -- | -- | -- | -- | -- | 8 | -- |
| plugin/ | -- | -- | 0 | -- | -- | 1 | 3 |
| contracts/ | -- | -- | 0 | -- | -- | 0 | 2 |

Totals: **10 IN-WORKER-FETCH rows** -- 8 in TELEMAC's own code (1 of them a
PRIVATE LADDER) plus 2 library-originated (rows 23, 24); **14 SERVER-BYPASS
paths**; **4 CAPABILITY-ONLY images**; **11 BUILD-TIME hosts**; **9 DEV-DRIVER
modules**; **1 UNRESOLVED** (canopy/geoai weight resolution); INERT proven at
every site examined for it. Zero rows in `plugin/` or `contracts/`.

By VISIBILITY PROPERTY, across all 24 fetch rows:

- EMIT: **0 rows.** Four rows (4-8, the bed COGs) carry a source string in a
  LAYER NAME via the ADR 0231 seam -- a label, not an emit-on-fetch row.
- LADDER (declared): **0 rows.** Three carry PRIVATE ladders (rows 4, 9, 12).
- CACHE (read-through + provenance sidecar): **0 rows.** Four have ad-hoc output,
  token or library caches (rows 16, 17, 20, 23).
- PROVENANCE sidecar: **0 rows.**
- RETRY DOCTRINE: 3 rows have backoff (4, 9, 23); 1 has mirrors without
  per-mirror retry (12). The remaining 20 are single-attempt.

Read that column-wise: across 24 external fetches, the substrate's five
properties are satisfied a combined **four times out of a possible 120**, and
never more than one property at a single site. This is not a set of leaks in a
mostly-sound surface -- outside `data/fetchers/`, the visibility machinery simply
does not exist.

**The two axes a source lint cannot cover.** Rows 23 and 24 fetch from inside a
dependency; rows 5-8's images ship `curl` and `gdal-bin` that no Python grep
sees. Every guard that reads our source is blind to both. That is the argument
for making network POSTURE, not source text, the load-bearing control.

## Proposed sweep guard -- PROPOSE, NOT IMPLEMENTED

Modelled on `tests/test_fallback_sweep_guard.py` and
`tests/test_law9_consequence_guard.py`: three enforcing layers, structural rather
than word-matching, with a REGISTER for what is knowingly parked. Proposed file:
`tests/test_external_fetch_sweep_guard.py`.

**Layer a -- STATIC LINT, greppable patterns.** Walk `workers/**/*.py` (non-test)
and `trid3nt_server/**/*.py` excluding `data/fetchers/_router/{transport,executors}/`
and `data/search/web_fetch/`, and fail on any module matching:

```
_NET_IMPORT   = r'^\s*(?:import|from)\s+(requests|httpx|urllib3|aiohttp|http\.client|
                 ftplib|pystac_client|planetary_computer|pooch|owslib|earthaccess|
                 cdsapi|siphon|dataretrieval)\b'
_NET_URLLIB   = r'urllib\.request\.(urlopen|Request|urlretrieve)'
_NET_URL      = r'["\']https?://(?!127\.0\.0\.1|localhost|0\.0\.0\.0)'
_NET_VSI      = r'/vsi(curl|s3|az|gs)/'      # flagged only when the operand is not our store
_NET_SHELL    = r'\b(curl|wget|aria2c)\b\s'  # inside a subprocess/list literal
_NET_SOCKET   = r'socket\.(create_connection|connect)\('
```

`_NET_URL` fires on comments and docstrings too. That is deliberate: the false
positives are cheap to allowlist by hand and the guard must not be defeatable by
building a URL in an f-string. The allowlist carries the reason, so an INERT
namespace URI is recorded as inert rather than silently skipped.

**Layer b -- THE ALLOWLIST, one shape, reason-bearing.** The ADR-0244 idiom:
a module-level dict whose value is the classification plus the sentence that
justifies it, and a companion test asserting every entry still matches, so a
site cannot change without changing this file.

```python
#: path (repo-relative) -> (marker, class, why)
#: class in {INERT, BUILD_TIME, DEV_DRIVER, LEGITIMATE, PARKED}
_EXTERNAL_FETCH_ALLOWLIST: dict[str, tuple[str, str, str]] = {
    "workers/openquake/job_ini.py": (
        'xmlns:gml="http://www.opengis.net/gml"', "INERT",
        "XML namespace identifiers written INTO generated NRML; never "
        "dereferenced, no schema validation is enabled in the worker."),
    "trid3nt_server/tools/search/web_fetch/web_fetch.py": (
        "httpx", "LEGITIMATE",
        "the declared open-web tool: reaching a model-named URL IS its "
        "contract."),
    "workers/telemac/telemac_river_dye_build.py": (
        "_fetch_dem_samples", "PARKED",
        "the private Copernicus->3DEP ladder + the NLDI centerline fetch; "
        "the TELEMAC family wave migrates these agent-side (rows 1-4)."),
    ...
}
```

Marker discipline is borrowed verbatim from the fallback guard: a stable
identifier, never whitespace-bearing, never a line number. `PARKED` entries must
carry a verdict sentence over ~80 chars so a placeholder cannot pass, and a
second test asserts every PARKED row names an audit row in this document.

**Layer c -- STRUCTURAL, the two that a lint cannot reach.**

1. *Manifest completeness.* For each engine that a PARKED row names, assert the
   worker manifest's `inputs` list is non-empty once the migration lands -- i.e.
   `steps/deck.py` no longer ships `"inputs": []` with a self-fetch comment. This
   is the test that turns the migration from a promise into a gate.
2. *Network posture.* Assert every `build_argv` in `trid3nt_server/` either
   includes `--network none` or names itself in a `_NETWORKED_SOLVERS` set with a
   reason. Today the set would be `{sfincs_quadtree}` (it needs `--network host`
   to reach MinIO), `{sfincs}` until row 23's catalog is baked, and `{telemac}`
   until rows 1-8 migrate. This is the ONLY layer with real teeth: a container
   with no network cannot grow a fetch silently, and the diff that opens the
   network is the diff a reviewer must justify.

**Layer c.2 is the recommendation, and rows 23 and 24 are why.** Layers a and b
read our source. Row 23 lives in `hydromt`'s default argument, row 24 in
`openquake.engine`'s version check, and rows 5-8's images ship `curl` and
`gdal-bin` that no Python grep sees. A lint over source is defeatable by any code
the lint has not seen; a network namespace is not. Concretely: `--network none`
would have failed the SFINCS build loudly on the first solve after the image was
built, instead of making `raw.githubusercontent.com` a silent per-solve
dependency nobody knew about.

Row 24 needs no guard at all, only `ENV CI=1` in `workers/openquake/Dockerfile`
-- but note it dispatches `exec`, on the host, so the env has to be set on the
subprocess, not the image. Worth a one-line test that the dispatch env carries
it.

## Migration shape per class

**IN-WORKER-FETCH and PRIVATE-LADDER (rows 1-8) -> agent-side Data + staged
input.** No new machinery: `manifest["inputs"] = [{"gs_uri": ..., "dest": ...}]`
is already staged into the rundir by `solver.py:978-1000` and lands in the
container through the existing `-v <rundir>:/data` mount. Per row:

- Rows 1-2 (NLDI centerline + re-seed) -> a declared `Data("centerline", ...)`
  producer routing a flowline fetcher, staged as a GeoJSON/FGB the worker reads
  instead of navigating NLDI. This ALSO repairs the false surface: the emitted
  layer becomes the geometry the mesh is built on. Whether the producer should be
  the existing OSM `fetch_river_geometry` or a new NHDPlus-navigation spec is a
  design fork for NATE -- they are different datasets and the answer determines
  what the canvas has been showing.
- Row 3 (NHDArea banks) -> the same treatment; the `bank_source` gate is already
  correct and simply moves upstream of the solve. Bank polygons become a
  publishable layer (already on NATE's queue).
- Row 4 (the private DEM ladder) -> DELETE the in-worker ladder; fetch the bed
  through `fetch_dem` / `fetch_topobathy` with a DECLARED rung. The
  Copernicus->3DEP swap then rides the same user-gated path as fallback-audit
  row 1 instead of happening silently. The DEM surfaces as a canvas layer with a
  coverage share, which is the thing NATE asked to SEE.
- Rows 5-8 (NOAA DEM_all x4) -> one shared agent-side topobathy fetch staged into
  all four builders, deleting four copies of the same twenty lines. `telemac3d`
  gains its first visibility of any kind.

**SERVER-BYPASS (rows 9-17) -> router adoption.** These are in-process already,
so adoption is a spec plus a call-site swap; they gain all five properties at
once. Priority order: row 9 (hot default path, and it deletes a `sys.path`
injection of `scripts/sandbox` into the daemon), then 10/11 (physics-consequential
source models), then 12/13 (fold the two extra NHDArea clients and the third
Overpass mirror list into the existing specs -- three deletions for one adoption),
then 14/16/17. Row 17 is the cheapest: `_pc_stac` already lives under `fetchers/`
and needs only to move onto `_router/transport/get_client()`, exactly as
`fetch_living_atlas_layer.py:99-114` already does.

**Rows 18-20 (discovery-shaped) -> transport adoption, not spec adoption.** Route
them through `_router/transport/` for the retry doctrine and a provenance record;
do not force them into a spec, because their contract is to reach a host the
model named.

**Row 15 (user-supplied URL) -> transport adoption + provenance.** The user named
the host, so it is visible by construction; what is missing is the record of what
came back.

**Rows 21-22 -> delete and document.** Row 21 has no caller: deletion-ledger
candidate. Row 22 is not ours to route, but the third-party host belongs in the
plugin's documented egress set.

**LIBRARY-ORIGINATED (rows 23, 24) -> configure the dependency, then deny the
network.** Row 23: bake the predefined catalog into `~/.hydromt_data` at image
build, or clear `model.data_catalog._fallback_lib` immediately after
constructing `SfincsModel` (the constructor exposes no `fallback_lib` param).
Row 24: `CI=1` on the OpenQuake dispatch env. Both are one-liners; both are
invisible to code review, which is the case for making them assertions.

**CAPABILITY-ONLY -> `--network none` plus image hygiene.** No code change; the
guard's layer c.2 turns a latent capability into an impossibility. The SFINCS
image is the exhibit twice over: static Dockerfile review called it clean, the
live `pip list` found transitive `requests` + `pooch`, and `pooch` turned out to
be the very thing row 23 fetches with.

**UNRESOLVED (canopy) -> close it before it is recorded either way.** Install
`geoai` and read its `cache_dir` resolver, or run one canopy solve with the
network denied and see whether it survives. The second is cheaper and is the
honest test.

## Top-5 worst offenders

Ranked by visibility severity against the five properties, weighted by whether
the fetched object is physics-consequential and by how hot the path is.

1. **Row 1 -- the TELEMAC river centerline (`telemac_river_dye_build.py:381-383,
   644-645`).** Scores ZERO on all five properties, on every river_dye and do_sag
   solve, and the fetched object IS the model domain. Worse than a hidden fetch:
   it is a FALSE SURFACE. The canvas shows an OSM Overpass river fetched purely to
   derive a seed point, while the mesh is built on an NLDI/NHDPlus navigation the
   user never sees. ADR 0231 records this row as SURFACED; it is not. A user
   comparing the visible layer to the result is comparing two different rivers.

2. **Row 9 -- `generate_mesh`'s coastal water edge
   (`generate_mesh.py:352-366` -> `scripts/sandbox/oceanmesh/water_edge.py`).**
   A `scripts/sandbox/` module `sys.path`-injected into the LIVE DAEMON and run
   in-process, carrying a raw `urllib` Overpass + NHD fetcher with its own private
   mirror ladder -- on the UNCONDITIONAL fallthrough default of `_infer_mode`.
   Directly contradicts ADR 0112's "coded data-fetchers = 0, one router" on a hot
   path, and it is a sandbox module that never graduated.

3. **Row 4 -- the private in-worker DEM ladder
   (`telemac_river_dye_build.py:1649-1695`).** A CROSS-DATASET substitution
   (Copernicus GLO-30 radar -> USGS 3DEP lidar) with its own retry policy,
   invisible to `registered_ladders()`, un-gated, un-measured. The agent-side
   twin of this exact swap is fallback-audit row 1, graded GOLD precisely because
   it is user-gated. The same decision, made in a container, is silent.

4. **Rows 5-8 -- the four copy-pasted NOAA DEM_all fetches
   (`{telemac_coastal,telemac3d,tomawac,artemis}_build.py`).** One bathymetry
   fetch duplicated four times, single-shot, no retry, no mirror, surfacing an
   untyped `HTTPError` with no `error_code`. This is the row that killed the
   tomawac showcase on NGDC 500s. `telemac3d` (row 6) is the single worst
   individual site in the audit: unconditional, physics-consequential, and it
   does not even write the bed COG the other three write, so it has no visibility
   of any kind.

5. **Row 23 -- SFINCS's hydromt catalog fetch (`_sfincs_build/deck.py:2526` ->
   `hydromt/data_catalog.py:75`).** Ranked fifth on severity, but FIRST on what it
   says about the guard. Every regular-grid SFINCS build -- the flood canary, the
   most-exercised engine in the product -- silently depends on
   `raw.githubusercontent.com` being reachable, through a default argument in a
   dependency, with no pre-warmed cache in the image. Nobody chose this; nobody
   could have found it by reading our code; and the first pass of this very audit
   graded the image CLEAN on exactly that reasoning. It is the proof that a
   source lint is the wrong primary control.

Just outside, and each would rank in a longer list:

- **Rows 10, 11 and 14 -- the fetched-model class** (`finite_fault.py:277`,
  `scenario_slab2.py:299`, `swmm_deck_runner.py:322`). One shape: the fetched
  object is not an input to the model, it IS the model -- the earthquake source,
  the slab geometry, the SWMM deck. Each is single-attempt with no cache, so
  reproducibility depends on a USGS endpoint or an `openswmm.org` forum thread
  being up and unchanged, and nothing records which version came back. Row 14 is
  deliberately un-cached for redistribution honesty, which makes the availability
  dependency permanent rather than incidental.
- **Row 24 -- the OpenQuake version beacon.** Not physics and not visibility, but
  a severity axis of its own: it is the only outbound telemetry in the system, it
  leaves the daemon HOST rather than a container, and its User-Agent reports the
  user's `calculation_mode` to a third party. Closed by one env var.
- **Row 21** -- a live Overpass-fetching HTTP route on the daemon with no caller
  anywhere in the repo.
- **The canopy caveat** -- unresolved rather than un-severe. If `geoai` does not
  find the baked weight filenames, a 749 MB solve-time pull from a public Meta
  bucket is happening today and this audit has not detected it.

Ranking criteria, stated so the order can be argued with: (i) does the fetched
object determine the physics, (ii) how hot is the path, (iii) how many of the
five visibility properties are absent, (iv) is the result actively MISLEADING
rather than merely invisible. Criterion (iv) is why row 1 leads: an invisible
fetch hides the truth, a false surface asserts a falsehood.
