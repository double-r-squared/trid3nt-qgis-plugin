# Bathymetry sources per water-body class (recon; for NATE sign-off BEFORE any implementation)

Chartered by the RUNG-3 SHAPE SETTLED second pass, item 4 (CORRECT-DATA-CLASS LAW)
and closed out by RUNG-3 CLOSE RESOLUTIONS item 1. The law says
`set_bed(source=DATA.topobathy)` takes the data class it is defined over: a DEM
(a water-SURFACE elevation where the channel is wet) is never a silent stand-in
for channel bathymetry. Wrong-class substitution is the author's explicit
declared choice, named in the journal, or the data row refuses honestly.

This document answers the three questions that law left open:

1. What REAL bathymetry exists, per water-body class, that a machine can fetch.
2. What honest SYNTHETIC supply looks like when no measurement exists, and how it
   is expressed as a declared PRODUCER rather than a shim.
3. What the outflow stage of a reach deck should hold once the bed is honest.

NOTHING here is implemented. This is a reading, not a plan.

---

## MethodologyForNate

Per the experiments-NATE-first law and the design-decisions-need-NATE-go law,
the following must carry NATE's explicit sign-off BEFORE any code is written.
Each is a DESIGN decision, not a mechanical one.

**M1. The ladder shape per water-body class.** Section 4 proposes three
ladders (coastal/estuary, navigable river, small inland stream) rather than one.
That is a structural choice: it means `fetch_topobathy` either grows a
water-body-class param or splits into sibling capabilities. NATE picks which,
and whether three classes is the right cut at all.

**M2. Whether a SYNTHETIC rung is permitted to exist.** Section 2 finds that
every no-measurement method is a regression or an inversion with a quantified,
often large, error band. Our `Consequence` vocabulary already has a `synthetic`
class inside `DEGRADATION_CLASSES`, so the machinery can express it loudly. But
the honest-refusal floor is a real alternative: refuse, and make the user supply
a bed. NATE rules whether a synthetic bed may be produced at all, and if so
whether it is opt-in only (never auto-descended) or a normal ladder rung.

**M3. The producer's method, named.** If M2 is yes, exactly which published
method the producer implements, since the producer's name states its method and
the name is a contract. Section 2 gives the candidates with their error classes.
This is a physics decision and the model never invents physics.

**M4. The outflow-stage decision.** Section 3 finds the community has no single
answer and that our current `outflow_stage = bed_out + init_depth_m` is a
declared depth on a measured bed. The candidate replacements each import a new
input (a slope, a rating curve, a gauge). NATE rules which, and whether the
current declared-depth form stays as the floor.

**M5. Verification status.** Several findings below are marked UNVERIFIED
because the fetch failed (TLS/403 in this sandbox) or because no primary source
was located. Those must be re-verified against primary text before any of them
becomes load-bearing in an implementation. NATE decides which are load-bearing.

**M6. No experiment runs from this document.** Any accuracy claim about a
producer we build (for example "the regression reproduces NXSDB cross-sections
to within X") is an EXPERIMENT. Methodology, input set, deterministic grading,
and run count go to NATE before a single run, per the standing law.

Verification tags used below: [VERIFIED] = primary source fetched and read this
session; [INDEXED] = URL and title confirmed via search, content not fetched;
[UNVERIFIED] = could not confirm, or no primary source located.

---

## 0. Where the repo stands today

Grounding facts, read from the tree this session, so the recommendation is
measured against what exists rather than against a memory of it.

- The existing bathymetry ladder is registered at
  `trid3nt_server/tools/fetchers/_router/hooks/topobathy.py` (the
  `BATHYMETRY_LADDER` near the file's end). Its rungs are `user_supplied`
  (`dem_uri`) -> `cudem_nearshore` (primary) -> `regional_fine` (enhancement)
  -> `etopo_bathy_base` (cross_dataset) -> `refuse`
  (`TOPOBATHY_COVERAGE_GAP`).
- That ladder is COASTAL-ONLY by construction: the delegate's validate hook
  refuses outside a US coastal envelope with the message "NOAA NCEI CUDEM is
  US-coast-only". There is no river rung on it at all.
- The consequence vocabulary in `trid3nt_server/fallbacks/ladder.py` already
  carries `synthetic` inside `DEGRADATION_CLASSES`, alongside `same_data` and
  `cross_dataset`. A declared synthetic producer is expressible today with no
  new machinery, and the loudness floor already keys on it.
- `fetch_nhdplus_hr_flowlines` already requests `totdasqkm` (total drainage
  area, km2) and `streamorde` as out_fields. Drainage area is therefore ALREADY
  fetchable for any US reach with no new fetcher.
- The deck's outflow stage is computed in
  `trid3nt_server/workflows/telemac/steps/author.py`:
  `bed_top = bed["bed_top_m"]`, `bed_out = bed_top - bed["bed_drop_m"]`,
  `outflow_stage = bed_out + P.init_depth_m`, with `init_depth_m` defaulting to
  2.0 m. The bed medians are measured on the accepted mesh at the declared
  boundary roles (`steps/deck.py`), which is the RUNG-3 close's "minimal
  measured bridge". The stage above that bed is a DECLARED depth.
- The deck writes `PRESCRIBED ELEVATIONS` with that number, in the measured
  liquid-boundary order.

So the open seam is precisely: the bed under the outflow is now measured, but
what the water surface sits at above it is still a labeled default.

---

## 1. Source inventory: what real bathymetry exists, per class

### 1a. Coastal and estuarine

**NOAA NCEI CUDEM (Continuously Updated Digital Elevation Model).** Integrated
topobathymetric tiles blending lidar, IfSAR and multibeam into seamless coastal
DEMs at 1/9 arc-second (about 3 m) and 1/3 arc-second (about 10 m). Coverage is
coastal-only and is a mosaic of discrete named regional tile-sets, not one
continuous national grid. Vertical datum is typically NAVD88 for CONUS tiles but
is stated PER TILE-SET in the metadata and is not uniform across releases,
especially for territories. Access: HTTPS directory listing at
https://chs.coast.noaa.gov/htdata/raster2/elevation/NCEI_ninth_Topobathy_2014_8483/
(third arc-second at
https://coast.noaa.gov/htdata/raster2/elevation/NCEI_third_Topobathy_2014_8580/),
mirrored on S3 under `noaa-nos-coastal-lidar-pds`, GDAL `/vsicurl/` against the
published VRT, or the Digital Coast Data Access Viewer at
https://coast.noaa.gov/dataviewer/. Format: GeoTIFF tiles plus shapefile tile
index plus VRT. [VERIFIED] This is what our `cudem_nearshore` rung already
reads.

**NOAA Coastal Lidar (`noaa-nos-coastal-lidar-pds`).** Raw and processed
topobathymetric lidar point clouds (LAZ) and derived DEMs from NOAA NGS and
JALBTCX green-laser surveys, named by project and year. Coverage is SCATTERED
PROJECT FOOTPRINTS along coastlines and estuaries, not continuous: confirmed
projects include Indian River Lagoon FL, Chesapeake Bay MD, Southern Tampa Bay
FL. Datum is stated per project, for example the Chesapeake Bay project is
explicitly "NAVD88 using GEOID18"
(https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/laz/geoid18/9424/index.html).
Access: `aws s3 ls --no-sign-request s3://noaa-nos-coastal-lidar-pds/`,
us-east-1, no account required; project index at
https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/laz/index.html; registry
entry https://registry.opendata.aws/noaa-coastal-lidar/; program page
https://coast.noaa.gov/digitalcoast/data/jalbtcx.html. [VERIFIED] Coverage is
opportunistic (post-storm surveys, targeted estuary studies), so availability
must be queried per site rather than assumed.

**NOAA BlueTopo / National Bathymetric Source (NBS).** The most important source
this research surfaced that we do not currently read. BlueTopo is NOAA's
compiled best-available gridded bathymetric surface for navigationally
significant US waters, built from BAGs, multibeam and other source surveys.
Product page https://nauticalcharts.noaa.gov/data/bluetopo.html, program page
https://nauticalcharts.noaa.gov/learn/nbs.html. [VERIFIED]

The decisive fact for us: **BlueTopo is on NAVD88**, an orthometric datum, and
is explicitly NOT on a navigational or tidal datum. That means it merges with
CUDEM and with 3DEP land elevation without a VDatum step, unlike raw NOS survey
BAGs (MLLW) or S-102 navigational surfaces. [VERIFIED]

Resolution is a multi-resolution UTM tiling scheme, depth-dependent (finer cells
in shallow and high-relevance water). The exact cell-size tiers were not
confirmed against primary text this session; the authoritative live answer is
the tile-index geopackage in the bucket. [UNVERIFIED as to specific tier values]

Access is unusually good: S3 bucket `noaa-ocs-nationalbathymetry-pds`
(https://registry.opendata.aws/noaa-bathymetry/) plus a purpose-built official
Python package and CLI, `nbs`, from
https://github.com/noaa-ocs-hydrography/BlueTopo, which resolves an AOI polygon
straight to the correct S3 tiles:
`fetch_tiles('/path/to/project', geometry='area_of_interest.gpkg')` or
`nbs fetch -d /path/to/project -g area_of_interest.gpkg`. Format is GeoTIFF with
float32 bands (elevation, uncertainty) plus an embedded Raster Attribute Table
carrying per-pixel contributor and quality metadata aligned to IHO standards.
[VERIFIED]

Coverage caveat: BlueTopo deliberately concentrates on navigationally
significant water, so a minor tidal creek or an undeveloped bay may have thin or
no coverage. The tile index is the coverage truth.

**NOAA NOS hydrographic survey BAGs.** The gridded survey product behind the
charts, in BAG (Bathymetric Attributed Grid) format, multi-band (depth plus
uncertainty). Post-1980 NOS surveys reference **MLLW**, confirmed at
https://www.ncei.noaa.gov/products/nos-hydrographic-survey. Discovery and
download through the NCEI Bathymetric Data Viewer,
https://www.ncei.noaa.gov/maps/bathymetry/. Coverage is coastal, scattered by
survey footprint. [VERIFIED as to datum and product] Whether a batch or REST API
exists beyond the interactive viewer was not confirmed. [UNVERIFIED as to
programmatic bulk access]

**NOAA ENC (Electronic Navigational Charts).** S-57 vector charts. Soundings and
depth contours are GENERALIZED FOR CHART DISPLAY, not survey-grade continuous
bathymetry. Download via https://www.nauticalcharts.noaa.gov/charts/noaa-enc.html.
[VERIFIED] Assessment: not a hydraulic-model bathymetry source. Useful for
reference and validation only. It should not appear on our ladder as a bed
source.

**USGS CoNED (Coastal National Elevation Database).** Region-by-region compiled
1 m topobathymetric DEMs, each a discrete USGS data release, typically extending
inland to +10 m elevation and offshore to the 3 nmi state-waters limit. Examples:
Northern California
(https://www.usgs.gov/data/topobathymetric-model-northern-california-1986-2019),
Central Coast California
(https://www.usgs.gov/data/topobathymetric-model-central-coast-california-1929-2017),
Southern California and Channel Islands
(https://www.usgs.gov/data/topobathymetric-model-southern-coast-california-and-channel-islands-1930-2014).
Program page:
https://www.usgs.gov/centers/eros/science/usgs-eros-archive-digital-elevation-coastal-national-elevation-database-coned.
[VERIFIED] Coverage is coastal, region-by-region where a compiled product
exists, not comprehensive. This is the same family our `regional_fine` rung
already reaches for.

### 1b. Navigable rivers and federal channels

**USACE eHydro.** The dredged-channel gold standard: hydrographic surveys of
federal navigation projects, covering an estimated 25,000 miles of federally
maintained channels, coastal and riverine
(https://www.cisa.gov/mts-resilience-resources/ehydro-usace-hydrographic-surveys).
[VERIFIED]

Access is a two-step pipeline, and this matters for any fetcher design. The
ArcGIS REST FeatureServer at
https://services7.arcgis.com/n1YM8pTrFmm7L4hs/arcgis/rest/services/eHydro_Survey_Data/FeatureServer
was confirmed live and queryable this session: a single layer `SurveyJob`
carrying survey outer-boundary POLYGONS with metadata including survey dates,
in EPSG:4326, max 2000 records per query, global extent spanning all US
districts including territories. [VERIFIED] The actual dense soundings are NOT
in that layer: they are per-survey bulk downloads in XYZ ASCII, File
Geodatabase, KMZ, or PDF plot sheets. So the honest shape is: query `SurveyJob`
for footprints and dates, then resolve each hit to its bulk download.
[VERIFIED as to the two-step shape; UNVERIFIED whether a sibling REST service
exposes soundings directly, worth checking the service directory at
https://services7.arcgis.com/n1YM8pTrFmm7L4hs/arcgis/rest/services/]

Hub listing:
https://geospatial-usace.opendata.arcgis.com/datasets?group_ids=df70bc1371e3468b902e657cd26fc91c.
The canonical portal https://navigation.usace.army.mil/Survey/Hydro returned a
TLS certificate error from this sandbox; the URL is corroborated across
secondary sources. [UNVERIFIED as to live status from here]

Datum is PROJECT-DEPENDENT, typically the local project vertical datum used for
channel maintenance, often MLLW in tidal reaches or a fixed project datum
inland. It must be read per survey and never assumed. Update frequency is
rolling, per district maintenance schedule, with dates carried on `SurveyJob`.

Coverage caveat that decides its ladder position: eHydro covers federally
maintained navigation channels ONLY. It is useless for a reach that is not a
federal channel, which is most reaches.

### 1c. Small inland streams

This is where the charter expected to find nothing, and the finding is more
interesting than that.

**USGS National Cross-Section Database (NXSDB).** Real, field-surveyed
bathymetric cross-sections measured by USGS hydrographers during routine
streamgaging site visits, the same visits that build stage-discharge ratings.
The Water Year 2023 release contains roughly 64,500 individual cross-sections at
8,556 gaging stations nationwide (WY2022: roughly 58,261 at 8,412 stations),
distributed as an annual national GeoPackage (`NXSDB_WY2023_CONUS.gpkg`) via
USGS ScienceBase: https://www.sciencebase.gov/catalog/item/669a7125d34e9ac16e167518
and the data-release page
https://www.usgs.gov/data/national-cross-section-database-nxsdb-water-year-2023-schema-version-120-september-2025.
[INDEXED; direct fetch of both pages returned 403 or a cert failure from this
sandbox, so the exact download URL needs re-verification, but the dataset's
existence, naming, scale and ScienceBase hosting are corroborated across
independent sources.]

Independent corroboration of scale and of its status as SURVEYED rather than
modelled geometry: a 2026 Water Resources Research paper, "Evaluating
Approximations of River Channel Shape Using a National Cross-Section Database",
uses 46,971 cross-sections from this dataset,
https://agupubs.onlinelibrary.wiley.com/doi/full/10.1029/2025WR041177.
[VERIFIED]

The answer to the charter's specific question is therefore: **yes, a typical
small non-navigable inland stream can have measured channel bathymetry, but only
at discrete points, and only where it hosts a USGS streamgage.** Most USGS gages
sit on exactly this class of stream, so the coverage is real. But NXSDB is
cross-section STATIONS, not a continuous bathymetric surface.

The scale limit, from Heldmyer et al. 2022 (HESS,
https://hess.copernicus.org/articles/26/6121/2022/): only about 2,800 of the
National Water Model's 2.7 million reach segments have measured channel
properties. [VERIFIED] For an arbitrary reach between gages there is no measured
bathymetry in any source reviewed here.

**USGS 3DEP topobathymetric (green) lidar.** Standard 3DEP lidar is near-national
but land-only. A much smaller subset uses green-wavelength lidar penetrating
clear shallow water, under the 3D National Topography Model inland-bathymetry
component: https://www.usgs.gov/3d-elevation-program/inland-bathymetry.
Confirmed named projects include the Potomac River (MD/VA/WV, 120-plus river
miles,
https://www.usgs.gov/news/technical-announcement/new-usgs-national-map-data-reveals-potomac-rivers-submerged-topography),
Niobrara River NE, Santiam River OR, Fish Springs NWR UT. [VERIFIED] Coverage is
SCATTERED PILOT PROJECTS, explicitly not national. Standard point clouds live in
`s3://usgs-lidar` (requester-pays) and `s3://usgs-lidar-public` (Entwine Point
Tile format, https://registry.opendata.aws/usgs-lidar/); discovery via
LidarExplorer, https://www.usgs.gov/tools/lidarexplorer. Whether topobathy-
specific projects are mirrored into the public EPT bucket rather than only
ScienceBase was not confirmed. [UNVERIFIED]

**What is NOT a bathymetry source, stated so it is not mistaken for one:**

- The National Water Model, HAND, and the OWP hydrofabric are DEM-DERIVED, not
  surveyed. HAND grids come from bare-earth DEMs and synthetic rating curves
  come from Manning's equation over DEM-extracted geometry
  (https://noaa-owp.github.io/hydrofabric/,
  https://github.com/NOAA-OWP/inundation-mapping/wiki). [VERIFIED] These belong
  in section 2 as synthetic methods, never on a ladder as measured bed.
- FEMA Base Level Engineering is a MODELING METHODOLOGY, not a data program.
  FEMA guidance
  (https://www.fema.gov/sites/default/files/documents/fema_base-level-engineering-analysis-mapping_112022.pdf,
  https://www.fema.gov/flood-maps/tools-resources/risk-map/base-level-engineering)
  builds models from a terrain surface and "may include channel surveys" only
  opportunistically where a community supplies them; the historical minimum data
  standard allowed 10 m or even 30 m DEMs. [VERIFIED] BLE is DEM-based by
  default and is not a bathymetry source.
- StreamStats provides basin and regression statistics, not surveyed
  cross-sections. [VERIFIED]

### 1d. State programs worth knowing

- **Texas TxGIO (formerly TNRIS) StratMap**: statewide lidar, free download at
  https://data.tnris.org, program page https://tnris.org/stratmap/elevation-lidar,
  spec at
  https://cdn.tnris.org/documents/state_of_texas_stratmap_lidar_specification_ver_XIII.pdf.
  Primarily topographic with targeted coastal bathymetry add-ons contracted by
  TWDB. [VERIFIED]
- **Florida FDEM statewide lidar**: QL1 topographic, catalogued via NOAA InPort
  https://www.fisheries.noaa.gov/inport/item/64526; discovery via LABINS
  https://www.labins.org/mapping_data/lidar/lidar.cfm and
  https://www.floridagio.gov/pages/terrestrial-lidar-links. Topobathy only where
  it overlaps USACE/FEMA post-storm projects, for example
  https://www.fisheries.noaa.gov/inport/item/49424 and the post-Ian 2022
  acquisition at https://portal.opentopography.org/noaaDataset?noaaID=9651.
  [VERIFIED]

### 1e. The datum bridge

Merging any MLLW-referenced source (NOS BAGs, much of eHydro in tidal reaches)
with a NAVD88 bed requires a vertical transformation. NOAA VDatum provides one,
and it has a live REST endpoint, confirmed this session:

Base: `https://vdatum.noaa.gov/vdatumweb/api/convert`. Required params `s_x`,
`s_y`; optional `s_v_frame`, `t_v_frame`, `s_h_frame`, `t_h_frame`, `s_z`,
`region`. Returns JSON with transformed `t_x`, `t_y`, `t_z` AND an uncertainty
value. Example: `https://vdatum.noaa.gov/vdatumweb/api/convert?s_x=-75.211&s_y=36.129`.
Docs: https://vdatum.noaa.gov/docs/services.html, project home
https://vdatum.noaa.gov/. [VERIFIED]

Two things follow. First, the transformation returns its OWN uncertainty, which
is a value our provenance should carry rather than discard. Second, that
uncertainty is not small everywhere: NOAA documents transformation uncertainties
in the Eastern Louisiana to Mississippi Sound regional model ranging from 20 to
50 cm at particular locations (https://vdatum.noaa.gov/). [VERIFIED] A 20 to 50
cm datum uncertainty is comparable to the bed features a reach model cares
about, so a datum-bridged rung is a real degradation and should be labeled as
one.

No batch or file endpoint was found on the REST API. [UNVERIFIED as to batch]

---

## 2. Synthetic channel methods, and how each would be expressed honestly

Every method here is a PRODUCER candidate. None is a shim. The architectural
point is that a producer's registered name states its method, and the journal
records that name, so a reader can always tell what made the bed.

### 2a. Thalweg burning and stream burning: rejected for our purpose

Stream burning lowers DEM cells along a mapped hydrography vector so that
flow-routing honors the known network. The canonical method is AGREE (Hellweger
1997, CRWR, University of Texas at Austin,
http://caee.webhost.utexas.edu/prof/maidment/gishydro/ferdi/research/agree/agree.html),
which applies a staged smooth re-slope across a buffer followed by a sharp
trench at the stream cells, specifically to avoid the parallel-stream artifact
that plain constant-offset burning produces. [VERIFIED]

The definitive critique is Lindsay, J.B. (2016), "The practice of DEM stream
burning revisited", Earth Surface Processes and Landforms 41(5) 658-668,
doi:10.1002/esp.3888, PDF at
https://jblindsay.github.io/ghrg/pubs/2016_Lindsay_ESPL.pdf. [VERIFIED] Its
findings that bear on us: the burn depth is chosen ad hoc and bears no relation
to true bathymetry; Jones (2002) notes a burned DEM is unsuited for measuring
local slope or curvature; scale mismatch between hydrography and grid produces
stream collisions and meander-cutoff artifacts.

**Assessment.** Stream burning enforces flow DIRECTION and CONNECTIVITY only. It
says nothing about cross-sectional conveyance, width or depth. It is a
hydrologic fix, not a hydraulic one. Under the correct-data-class law it is
precisely the confusion the law forbids: an offset chosen for routing reasons,
silently read as a depth. **Recommendation: never a rung on the topobathy
ladder.** If it appears at all it belongs in mesh or terrain preparation, under
its own name, never as a bed source.

### 2b. Hydraulic geometry regressions

**Leopold and Maddock (1953)**, USGS Professional Paper 252, "The Hydraulic
Geometry of Stream Channels and Some Physiographic Implications",
https://pubs.usgs.gov/publication/pp252. Downstream hydraulic geometry:
`w = a*Q^b`, `d = c*Q^f`, `v = k*Q^m`, with `a*c*k = 1` and `b+f+m = 1` by
continuity. Typical downstream exponents b about 0.5, f about 0.4, m about 0.1;
at-a-station values differ, commonly b about 0.26, f about 0.40, m about 0.34
(USGS WSP 1539-W, https://pubs.usgs.gov/wsp/1539w/report.pdf). [VERIFIED]
Error class: empirical regional fits with substantial scatter, not a universal
law; the original paper gives no formal confidence interval and later authors
treat b, f, m as regionally variable. Inputs: a fixed-frequency or bankfull
DISCHARGE at the section, which an ungauged DEM-only reach does not have without
importing a flood-frequency regression.

**Regional bankfull curves keyed on DRAINAGE AREA.** Dunne and Leopold (1978)
proposed substituting drainage area for discharge precisely because area is
derivable from a DEM everywhere including ungauged reaches. Same power form:
bankfull width, mean depth and area equal `a*DA^b`. Compilations: USGS
https://pubs.usgs.gov/wri/2003/4014/wri20034014.pdf and
https://pubs.usgs.gov/sir/2005/5153/pdf/Bankfull_book.pdf; EPA appendix
https://www.epa.gov/sites/default/files/2015-08/documents/appendix-a_hydraulic_regional_curves.pdf.
[VERIFIED]

**Bieger et al. (2015)**, "Development and Evaluation of Bankfull Hydraulic
Geometry Relationships for the Physiographic Regions of the United States",
JAWRA 51(3), doi:10.1111/jawr.12282, PDF read directly at
https://swat.tamu.edv/media/114657/bieger_etal_2015.pdf. [VERIFIED] This is the
canonical national-scale version and the strongest candidate for a producer.
Compiled from about 50 publications and 1,861 sites, reduced to 1,310 usable
sites across CONUS. Fits `y = a*DA^b` by log-log regression, both CONUS-wide and
per Fenneman-Johnson physiographic division (and per province where sample size
allowed), for bankfull width (m), depth (m) and cross-sectional area (m2).

Error is reported as R2 and standard error of estimate in log space, framed
explicitly as a MULTIPLICATIVE band once back-transformed: an SEE of 0.15 gives
a one-sigma band of times 1.41 and divided by 1.41. Two findings matter for
honesty: regional division-level curves are more reliable than the single
nationwide curve, and drainage area is explicitly shown to be a LESS RELIABLE
predictor than bankfull discharge. Substituting area for discharge is a
quantified additional source of scatter, not a free lunch. Inputs: drainage area
and a physiographic-region lookup, nothing else.

**Relevance to our stack:** `fetch_nhdplus_hr_flowlines` already returns
`totdasqkm`. NHDPlus HR value-added attributes additionally carry `QAMA` (mean
annual flow from runoff, cfs) and `VAMA` (velocity for QAMA, fps), documented in
the USGS NHDPlus HR user's guide, SIR 2025-5031,
https://pubs.usgs.gov/publication/sir20255031/full, product page
https://www.usgs.gov/national-hydrography/nhdplus-high-resolution. [VERIFIED]
So both the drainage-area form and the discharge form are input-satisfiable
today with no new fetcher. Note also that `QAMA/VAMA` gives a cross-sectional
AREA directly by continuity, which is a third and more direct path than either
regression.

**National Water Model channel parameters.** WRF-Hydro represents each reach as
a compound trapezoid: `BtmWdth`, `TopWdth`, `ChSlp` for the main channel and
`TopWdthCC` for the compound floodplain, with `n` and `nCC` roughness
(https://wrf-hydro.readthedocs.io/en/readthedocs/model-physics.html). For NWM
v2.0 and v2.1 these were populated by CONUS regressions against contributing
area following Bieger et al. as refined by Blackburn-Lynch et al. (2017)
stratified by Hydrologic Landscape Region. [VERIFIED]

The honest evaluation of that approach is Heldmyer et al. (2022), "Evaluation of
a new observationally based channel parameterization for the National Water
Model", HESS 26, 6121-6141, https://hess.copernicus.org/articles/26/6121/2022/.
[VERIFIED] Replacing the CONUS regression with a regionalized fit from about
48,000 gauges and 2.8 million measurements gave HUC2-level cross-validation R2
of 0.12 to 0.66 (median 0.37), and raised median streamflow R2 across about
7,400 gauges only from 0.479 to 0.494, a 3.1 percent gain. A Sobol sensitivity
analysis found Manning's n dominates simulated-flow sensitivity over channel
geometry.

**Error class for the whole regression family:** large, region-dependent
scatter; multiplicative rather than additive error; a second-order control on
simulated flow relative to roughness, meaning geometry error is PARTIALLY but
not fully absorbed by roughness calibration.

### 2c. Conveyance-preserving fits, and the quantified cost of getting it wrong

The generic problem: given a top width and a regressed depth, choose a shape
(trapezoid, parabola, rectangle) such that composite channel-plus-floodplain
conveyance matches a true surveyed section across a range of stages.

HEC-RAS frames this as TERRAIN MODIFICATION rather than shape optimization: the
RAS Mapper tools let a modeler build cross sections and interpolate a channel
surface between them, or draw bank lines and a thalweg elevation profile that
get burned into the terrain raster
(https://www.hec.usace.army.mil/confluence/rasdocs/rmum/latest/terrain-modification,
https://www.hec.usace.army.mil/confluence/rasdocs/hgt/latest/tutorials/terrain/terrain-modification).
[VERIFIED] The guidance ASSUMES some real soundings to anchor interpolation and
is silent on fully synthetic reaches. It is a terrain-editing utility, not an
estimator.

Quantified consequences of a wrong or absent channel:

- **Neal et al. (2021)**, "Estimating River Channel Bathymetry in Large Scale
  Flood Inundation Models", Water Resources Research, doi:10.1029/2020WR028301,
  https://www.fathom.global/academic-papers/estimating-river-channel-bathymetry-in-large-scale-flood-inundation-models/.
  Prior uniform-flow (implicitly prismatic-trapezoid) estimation carries an
  OVER-PREDICTION BIAS and is inaccurate for backwater-affected profiles.
  Substituting gradually varied flow theory, matching the synthetic channel to
  the actual non-uniform water-surface profile, **reduced model error against a
  target profile by 66 percent** and eliminated the bias, at the cost of a
  significant reduction in flood extent and floodplain storage relative to the
  biased baseline. [VERIFIED]
- **Dey et al. (2022)**, "Incorporating Network Scale River Bathymetry to
  Improve Characterization of Fluvial Processes in Flood Modeling", WRR 58(11),
  doi:10.1029/2020WR029521,
  https://agupubs.onlinelibrary.wiley.com/doi/full/10.1029/2020WR029521. A
  symmetric-trapezoid or bathymetry-absent simplification produces spurious
  backwater flow, inaccurate water-table elevation and incorrect inundation
  extent, especially on LOWER-ORDER streams. Shape fidelity matters, not just
  width and depth. [VERIFIED as to the qualitative finding]
- **Muthusamy et al. (2021)**, "Understanding the effects of Digital Elevation
  Model resolution in urban fluvial flood modelling", Journal of Hydrology 596,
  https://www.sciencedirect.com/science/article/pii/S0022169421001359.
  Coarsening 1 m to 50 m raised flood extent from 58.9 to 79.0 ha (plus 30
  percent) and mean flood depth from 1.74 to 4.30 m (plus 150 percent), directly
  attributed to loss of channel definition and reduced conveyance once cell size
  exceeds channel width. A MERGED DEM, high-resolution channel geometry embedded
  in a coarse floodplain DEM, cut mean-depth error from 90 percent to about 4 to
  5 percent and RMSE from 2.6 m to 0.9 m at 30 m resolution. [VERIFIED]

**The consolidated error class, and the single most important sentence in this
document:** the dominant error mode of DEM-only (water-surface) terrain is
SYSTEMATIC UNDER-REPRESENTATION OF CHANNEL CONVEYANCE, producing OVER-PREDICTION
of inundation extent and depth. It is a bias, not unbiased noise. Its magnitude
grows sharply once grid resolution approaches or exceeds true channel width, and
is worst on lower-order streams. This is exactly why the correct-data-class law
is right, and it gives the law a number rather than a principle.

### 2d. HAND and synthetic rating curves

Zheng, Tarboton, Maidment, Liu and Passalacqua (2018), "River Channel Geometry
and Rating Curve Estimation Using Height above the Nearest Drainage", JAWRA
54(4) 785-806, doi:10.1111/1752-1688.12661, PDF read at
https://digitalcommons.usu.edu/cgi/viewcontent.cgi?params=/context/cee_facpub/article/4603/&path_info=CEEfacpub2018_ZhengTarbotonMaidment_RiverChannelGeometry.pdf.
[VERIFIED] Method: HAND gives each cell its elevation above the nearest drainage
cell; incrementing a synthetic stage and intersecting with the HAND raster
yields wetted area, wetted perimeter and hydraulic radius directly from terrain
at each stage, not from a fitted trapezoid; Manning's equation then maps stage
to discharge, building a synthetic rating curve per reach. Validated against
calibrated HEC-RAS models and USGS gauges on the Blanco River TX and Tar River
NC at 10 m DEM resolution. HAND plus hydraulic property tables:
https://www.osti.gov/biblio/1608331, https://www.osti.gov/biblio/1630903.

**The caveat baked into the method, and it is our exact problem:** because the
DEM has no bathymetry below the water surface at acquisition time, the channel
bottom HAND uses IS effectively the water-surface elevation at DEM-acquisition
flow. HAND-derived geometry therefore systematically UNDERESTIMATES in-channel
conveyance below that reference stage, corrected in practice only by adding an
assumed bathymetric offset. [VERIFIED] Roughness is then used to compensate for
the residual conveyance deficit, which is why n is repeatedly the dominant
calibration lever.

### 2e. Observation-inversion methods

**SWOT-era river bathymetry inversion**, the family closest to the TELEMAC and
Mascaret lineage. Mass-conserved flow-law inversion and variational data
assimilation jointly estimate discharge, an EFFECTIVE bathymetry and friction
from repeat remotely sensed water-surface elevation, slope and width, with no
in-situ soundings. Durand et al. (2023), "A Framework for Estimating Global
River Discharge From the Surface Water and Ocean Topography Satellite Mission",
WRR, doi:10.1029/2021WR031614. Larnier, Monnier et al., "River discharge and
bathymetry estimation from SWOT altimetry measurements",
https://www.semanticscholar.org/paper/af63bc60acaa2d7ad01b38cf2e815d9a8764a14c.
Larnier et al. (2025), "Estimating Channel Parameters and Discharge at River
Network Scale Using Hydrological-Hydraulic Models, SWOT and Multi-Satellite
Data", WRR, doi:10.1029/2024WR038455. [VERIFIED as to existence and framing]

Critical property: the inversion is ILL-POSED from the flow equations alone and
requires prior information, typically a hydraulic-geometry width-depth prior, to
regularize (Larnier et al. 2020). The recovered bed is an EFFECTIVE bed
consistent with observed water-surface dynamics, not a true bed; it absorbs
friction uncertainty into the bed elevation unless friction is independently
constrained. The same ill-posedness is confirmed by Liu et al. (2024),
"Bathymetry Inversion Using a Deep-Learning-Based Surrogate for Shallow Water
Equations Solvers", WRR, doi:10.1029/2023WR035890, which must PRESCRIBE
roughness, discharge and width to make bed elevation solvable. [VERIFIED]

**Satellite-derived bathymetry (SDB)** for optically clear shallow coastal water.
Two classical algorithms: Lyzenga log-linear multi-band regression, and Stumpf,
Holderied and Sinclair (2003), Limnology and Oceanography 48(1), log-ratio
`Z = m1*[ln(nL(lambda1))/ln(nL(lambda2))] - m0`. Quantified accuracy from Md
Said, Mahmud and Che Hasan (2017), ISPRS Archives XLII-4/W5,
doi:10.5194/isprs-archives-XLII-4-W5-159-2017,
https://isprs-archives.copernicus.org/articles/XLII-4-W5/159/2017/isprs-archives-XLII-4-W5-159-2017.pdf:
calibrated Stumpf RMSE 1.432 m and Lyzenga RMSE 1.728 m against 2,452
single-beam echosounder check depths. [VERIFIED] NOAA operates an SDB pipeline
(NGS SatBathy,
https://geodesy.noaa.gov/web/science_edu/webinar_series/noaa-satbathy-tool.shtml)
and publishes an ICESat-2-validated product on AWS Open Data,
https://registry.opendata.aws/noaa-nos-scuba-icesat2-pds/. [VERIFIED]

Error class: depth- and turbidity-dependent, saturating in deep or turbid water.
**Crucially, SDB is not a bathymetry-free method: it still needs known soundings
to fit m0 and m1.** It is a coastal and estuarine method, not applicable to
typical inland fluvial channels.

**"BathyMet" as a named method was not located in the peer-reviewed literature
searched.** [UNVERIFIED / not found] The closest verified analog is Neal et al.
(2021) above, which is explicitly a Manning's and gradually-varied-flow
bathymetry estimator.

### 2f. What TELEMAC itself does

Bed elevation for a TELEMAC-2D or 3D SELAFIN geometry file is produced by
interpolating SCATTERED bathymetric point data onto mesh nodes, historically via
STBTEL and now via the `pretel/interpolation` module,
http://docs.opentelemac.org/notebooks/v8p2r0/pretel/interpolation.html, using an
Inter2D class offering linear, cubic and nearest-neighbour scattered-data
interpolators. [VERIFIED] Like HEC-RAS, the machinery is AGNOSTIC TO POINT
PROVENANCE. It will happily interpolate synthetic points. That is exactly why
the correct-data-class law has to live at OUR layer: neither engine will refuse
a bad bed on our behalf.

No TELEMAC User Conference paper specifically on synthetic bathymetry was
located. [UNVERIFIED / not found]

### 2g. How a synthetic method is expressed honestly in our architecture

Stated so M3 has something concrete to rule on. A synthetic bed is a PRODUCER
under its real method name, registered like any processing tool, appearing on
the ladder as a rung with `consequence="synthetic"`, which already sits inside
`DEGRADATION_CLASSES` and therefore already trips the loudness floor and the
gate card. Sketch, in the declaration vocabulary:

```
topobathy = tool("fetch_topobathy", bbox=...).ladder(
    tool("fetch_ehydro_survey", bbox=...),
    tool("synthesize_bankfull_channel_bieger2015",
         flowlines=DATA.flowlines, terrain=DATA.terrain),
)
```

Three properties make that honest rather than a shim. The producer's NAME states
its method and its citation, so the journal entry is self-describing. Its
`describes` text carries the error class in the words section 2b gives it, so
the gate card tells the user what they are accepting. And it is a rung, so
descending to it is a recorded, labeled event rather than a silent substitution
inside a fetch. What it must never be is a default inside `fetch_topobathy` that
fills a gap without a rung record.

---

## 3. The outflow-stage question

### 3a. What TELEMAC-2D actually offers

Sourcing caveat: opentelemac.org returned TLS certificate errors on every fetch
attempt from this sandbox, so the keyword spellings below are corroborated
across independently fetched sources that quote the manual verbatim, principally
the hydro-informatics.com TELEMAC-2D teaching modules
(https://hydro-informatics.com/telemac2d-steady,
https://hydro-informatics.com/telemac2d-unsteady). The manual itself is at
https://www.opentelemac.org/downloads/MANUALS/TELEMAC-2D/telemac-2d_user_manual_en_v7p0.pdf.
[INDEXED, not fetched]

**PRESCRIBED ELEVATIONS** (French `COTES IMPOSEES`). Semicolon-separated, one
value per liquid boundary, in `.cli` boundary order:
`PRESCRIBED ELEVATIONS : 374.80565;371.33`. [VERIFIED via quoting source] This
is the constant-in-time case, and it is exactly what our deck writes today. For
time-varying stage, TELEMAC reads the file named by `LIQUID BOUNDARIES FILE`. A
third path is the user Fortran subroutine `BORD`.

**STAGE-DISCHARGE CURVES** and **STAGE-DISCHARGE CURVES FILE**:

```
STAGE-DISCHARGE CURVES : 0;1
STAGE-DISCHARGE CURVES FILE : 'rating_curve.txt'
```

Value 0 deactivates for that boundary. **Value 1 applies prescribed elevations
as a function of computed discharge, that is Z(Q), the classic downstream rating
curve. Value 2 applies prescribed flow rates as a function of computed
elevation, Q(Z)**, for weir- or structure-governed outlets. [VERIFIED via
quoting source] The file is plain ASCII; TELEMAC binds a curve to a boundary by
the boundary index in the column header, for example `Z(2)` and `Q(2)` for the
second liquid boundary. Column order does not matter; the integer decides the
direction of the lookup.

**Boundary type codes** `LIHBOR`, `LIUBOR`, `LIVBOR`, `LITBOR` in the `.cli`
file, mirrored in the CRAN telemac package docs
(https://rdrr.io/cran/telemac/man/read_cli.html): 4 is free or unspecified
(zero-gradient release for velocity), 5 is prescribed depth via `HBOR` or
prescribed flowrate, 6 is prescribed velocity via `UBOR`/`VBOR`, and 2 on
velocity is a friction-law boundary via `AUBOR`. A free-surface-imposed outflow
is therefore `LIHBOR = 5`, `LIUBOR = 4`, `LIVBOR = 4`. [VERIFIED as to code
meanings] The exact left-to-right column ordering in a given `.cli` export is
exporter-dependent (Fudaa PrePro and BlueKenue being the two standard editors).
[UNVERIFIED as to column order]

**OPTION FOR LIQUID BOUNDARIES**, one integer per open boundary: 1 is the normal
treatment, **2 activates the Thompson method of characteristics**. Thompson
computes a physically consistent velocity from the characteristics of the
shallow-water equations rather than accepting a literal prescribed velocity, and
makes small adjustments to cancel inconsistencies when a boundary is
over-specified. It is recommended where a simple prescribed-elevation boundary
produces numerical reflection or is over-constrained: outlets near critical
flow, tidal and open-sea boundaries. [VERIFIED as to keyword and semantics]
Historically unavailable in parallel runs before release 6.1. [UNVERIFIED for
current releases]

### 3b. What practitioners actually hold the downstream stage at

**Normal depth from a friction slope.** HEC-RAS exposes exactly three downstream
options for a 2D area
(https://www.hec.usace.army.mil/confluence/rasdocs/r2dum/latest/boundary-and-initial-conditions-for-2d-flow-areas/external-boundary-conditions,
https://www.hec.usace.army.mil/confluence/display/RASSED1D/Downstream+Boundary+Conditions):
Stage Time Series, Rating Curve, and Normal Depth. Normal Depth "can only be
used to take flow out of a 2D flow area" and takes exactly ONE parameter, the
friction slope, which HEC-RAS plugs into Manning's equation against the
cross-section under the boundary line to back-calculate a water-surface
elevation per computed flow, evaluated per cell along the boundary. The friction
slope is commonly set to the local bed slope near the boundary. [VERIFIED]
HEC-RAS guidance elsewhere is explicit that this or a rating curve is preferred
for unsteady runs because it "will allow for a changed stage as the flow
hydrograph changes"
(https://www.hec.usace.army.mil/confluence/rasdocs/hgt/latest/tutorials/2d-unsteady-flow/floodway-encroachment-analysis-2d).
[VERIFIED]

A caution worth carrying: the HEC-RAS sediment documentation warns Normal Depth
"can introduce troubling numerical feedback" because it decouples bed elevation
from water-surface elevation, and confines its recommended use to reaches near
morphological quasi-equilibrium. [VERIFIED] Normal depth is a numerically
convenient fiction, not a measured boundary.

**A gauge rating curve or a measured stage hydrograph.** Standard practice where
a gauge sits at or near the outlet; HEC-RAS's Stage Time Series option is
explicitly called the best choice for historical hindcasting. The standing
caution is that rating uncertainty propagates into the model: an ASCE Journal of
Hydraulic Engineering study on sand-bed rivers reports up to 25 percent
reduction in calculated flow depth when questionable rating-curve discharges are
used as input,
https://ascelibrary.org/doi/10.1061/(ASCE)HY.1943-7900.0000362. [VERIFIED]

**Move the boundary far enough away that it does not matter.** Three consistent
quantifications of the backwater influence length were found. The peer-reviewed
anchor is the length scale `L ~ h/S`, depth over bed slope, attributed to Paola
and Mohrig (1996) and reviewed in "Backwater length estimates in modern and
ancient fluvio-deltaic settings", https://www.sciencedirect.com/science/article/pii/S0012825224000199.
[VERIFIED] The UK Environment Agency Fluvial Design Guide operationalizes it as
`L = 0.7 * h / S`
(https://assets.publishing.service.gov.uk/media/602ea199d3bf7f7220fe10b8/_Fluvial_Design_Guide_Technical_Report.pdf).
[INDEXED via search-quoted snippet, primary chapter unreachable] Aquaveo's
practitioner guidance is two floodplain widths upstream and downstream of the
area of interest, refined by sensitivity testing
(https://aquaveo.com/blog/post/best-practices-2d-hydraulic-modeling). [VERIFIED
as vendor guidance, not peer-reviewed]

**FEMA guidance.** The relevant document is "Guidance for Flood Risk Analysis
and Mapping: Hydraulics - Two-Dimensional Analyses" (Nov 2023),
https://www.fema.gov/sites/default/files/documents/fema_rm-hydraulics_2d_analyses_guidance_nov_2023.pdf.
The fetch returned only the compressed PDF byte stream, not extractable text, so
**FEMA's specific downstream-stage recommendation language could not be verified
against primary text.** [UNVERIFIED beyond document existence and title] If
FEMA's exact wording becomes load-bearing it needs a direct PDF-to-text pass.

### 3c. What happens when the bed is synthetic or DEM-derived

Published practice defaults pragmatically to **friction slope at normal depth**,
and the reason is architecturally interesting: normal depth needs NO EXTERNAL
STAGE DATA AT ALL. It is internally consistent with whatever bed and slope the
model already has, which sidesteps the datum question entirely. This phrasing
recurs in DEM-based HEC-RAS 2D studies using synthetic or interpolated
bathymetry, including the Dey et al. 2022 line of work
(https://agupubs.onlinelibrary.wiley.com/doi/full/10.1029/2020WR029521) and
"River Bathymetry Model Based on Floodplain Topography", Water 2019,
https://doi.org/10.3390/w11061287. [VERIFIED as to the practice]

The datum-mismatch failure mode the charter asked about, a gauge-referenced
stage being inconsistent with a synthetic or DEM-derived bed, **is a real and
logically obvious failure mode, but no primary source naming and analysing it
directly was located** despite targeted searching. [UNVERIFIED / not found] The
safest inference from what WAS found is that normal depth is the community's
practical default precisely because it avoids needing an external, potentially
mismatched stage datum when the bed itself is synthetic. That inference should
be labeled as an inference, not cited as a finding.

### 3d. TELEMAC community practice

The community default for a simple non-tidal river reach is **PRESCRIBED
ELEVATIONS**, constant for steady calibration runs or a `LIQUID BOUNDARIES FILE`
time series for unsteady runs; escalating to **STAGE-DISCHARGE CURVES option 1,
Z(Q)**, where a real rating curve exists at or near the outlet; and to
**Thompson, OPTION FOR LIQUID BOUNDARIES = 2**, where the simple boundary
reflects or is over-constrained. [VERIFIED as to the escalation pattern, via the
teaching material]

Downstream-boundary configuration is a recurring, not-canonically-answered
community question. Confirmed-existing forum threads by title:
https://www.opentelemac.org/index.php/assistance/forum5/16-telemac-2d/4024-downstream-boundary-condition-rating-curve,
https://www.opentelemac.org/index.php/assistance/forum5/16-telemac-2d/12283-free-water-level-on-downstream-boundary,
https://www.opentelemac.org/index.php/assistance/forum5/16-telemac-2d/12862-downstream-boundary-condition,
https://www.opentelemac.org/index.php/community-old/forum4/16-telemac-2d/10545-problem-with-rating-curve,
and on Thompson,
https://www.opentelemac.org/index.php/community-old/forum4/16-telemac-2d/4321-questions-about-thompson-boundary-condition.
**Thread CONTENT could not be fetched (TLS blocked every opentelemac.org URL);
these are cited as confirmed-existing threads whose titles indicate the topic,
never as sources for specific advice.** [INDEXED]

No TELEMAC User Conference paper narrowly about downstream-boundary selection
methodology for river reaches was located. The TUC index is at
https://www.opentelemac.org/index.php/user-conference; one volume,
https://www.vliz.be/imisdocs/publications/321522.pdf, exceeded the fetch size
limit before its contents could be searched. [UNVERIFIED / unexplored]

### 3e. What this means for our deck

Reading our current form against the above: `outflow_stage = bed_out +
init_depth_m` with a 2.0 m default is, in TELEMAC terms, a constant `PRESCRIBED
ELEVATIONS` on a measured bed. It is honest in that it is a labeled default over
a measured artifact fact, and it imports no external data that could be
datum-mismatched. Its weakness is that 2.0 m is not a property of the reach: it
is the same number on a mountain creek and a coastal plain river.

The candidate replacements, in ascending order of imported input, are laid out
in section 4c for M4. None of them is proposed here as the answer.

---

## 4. A recommended ladder for the DATA.topobathy row

Proposed for M1. Every rung named below is either a real fetchable source from
section 1 or a declared producer from section 2. The terminal rung is always
REFUSE, per the existing ladder contract.

### 4a. Coastal and estuary

```
user_supplied      (dem_uri)          the caller's own survey or grid
bluetopo           primary            NOAA NBS BlueTopo, NAVD88, multi-res UTM
cudem_nearshore    primary/alternate  NOAA NCEI CUDEM 1/9", NAVD88, ~3 m
regional_fine      enhancement        NOAA/USGS CoNED regional ~1 m
nos_bag_mllw       cross_dataset      NOS survey BAG, MLLW, VDatum-bridged
etopo_bathy_base   cross_dataset      ETOPO 2022 15", ~450 m, EGM2008
refuse             refuse             TOPOBATHY_COVERAGE_GAP
```

Changes from the ladder we have. **BlueTopo is the significant addition**: it is
on NAVD88, it has an official AOI-to-tiles fetch tool, and it carries a
per-pixel uncertainty band and source-survey attribution in its RAT, which is
better provenance than anything currently on the ladder. Whether it outranks
CUDEM or sits beside it is a resolution-versus-coverage question that depends on
the AOI, and is part of M1.

`nos_bag_mllw` is marked `cross_dataset` rather than `primary` deliberately: it
requires a VDatum transformation whose own reported uncertainty reaches 20 to 50
cm in some regions, which is a real degradation and should be labeled as one.
The VDatum uncertainty value should be carried into provenance, not discarded.

ENC does not appear. It is cartographic, not survey-grade, and putting it on a
bed ladder would be a category error.

### 4b. Navigable river and federal channel

```
user_supplied       (dem_uri)          the caller's own survey
ehydro_survey       primary            USACE eHydro channel survey, project datum
bluetopo            same_data/altern.  where the reach is navigationally significant
topobathy_lidar     enhancement        USGS 3DEP green lidar, where a project exists
nxsdb_sections      cross_dataset      USGS surveyed cross-sections at gages
synthesize_channel  synthetic          a declared producer, per M2/M3
refuse              refuse             TOPOBATHY_COVERAGE_GAP
```

eHydro is primary because it IS the dredged-channel gold standard, but three
properties shape how it must be implemented. It is a two-step fetch (query
`SurveyJob` for footprints and dates, resolve to a bulk XYZ or GDB download).
Its datum is PROJECT-DEPENDENT and must be read per survey, never assumed, which
means a per-survey datum gate analogous to the CUDEM NAVD88 gate the topobathy
hook already runs. And it covers federal channels ONLY, so its coverage fraction
over an arbitrary AOI will frequently be zero or partial, which the existing
`_rung_coverage` machinery already knows how to measure and report.

`nxsdb_sections` is `cross_dataset` rather than `primary` because it is a
fundamentally different SHAPE: point cross-sections, not a surface. Turning
sections into a bed requires interpolation along the reach, which is itself a
modelling choice and arguably belongs in a producer rather than a fetcher. That
is a real design question for M1.

### 4c. Small inland stream

```
user_supplied       (dem_uri)          the caller's own survey
nxsdb_sections      primary            USGS surveyed cross-sections, where a gage sits on the reach
topobathy_lidar     enhancement        USGS 3DEP green lidar, scattered pilot reaches
synthesize_channel  synthetic          a declared producer, per M2/M3
refuse              refuse             TOPOBATHY_COVERAGE_GAP
```

This is the class the charter expected to be empty, and NXSDB makes it not
empty. But the honest framing is narrow: NXSDB gives measured geometry only
where a USGS gage sits on the reach, and Heldmyer et al. put the network-wide
scale at roughly 2,800 measured reaches against 2.7 million. For an arbitrary
reach between gages the ladder falls to the synthetic rung, or to REFUSE if M2
rules that no synthetic bed may be produced.

**The honest-refusal floor.** For every class, REFUSE is the terminal rung and
means what it says: raise `TOPOBATHY_COVERAGE_GAP` naming the gap, and let the
author supply a bed or accept a labeled synthetic one explicitly. What must
NEVER happen, and what the correct-data-class law exists to prevent, is a
land-DEM leg quietly painting the wet channel and the result being called
topobathy. Section 2c gives that prohibition its number: DEM-only channels
under-convey systematically, over-predicting depth by up to 150 percent at
coarse resolution in the Muthusamy case, and the bias is worst on exactly the
small streams where our synthetic rung would otherwise be most tempting.

### 4d. If a synthetic producer is approved

For M3, the recommendation with reasons, offered as a proposal and not a
decision:

The strongest candidate is a **Bieger et al. (2015) physiographic-division
bankfull regression**, because its inputs are already fetchable (`totdasqkm`
from `fetch_nhdplus_hr_flowlines`), its error is published as a multiplicative
SEE band per region that the producer can state honestly in its `describes`
text, and the paper itself is explicit that the drainage-area form is weaker
than the discharge form, which is a caveat we can surface rather than hide.

A second and possibly better path, worth NATE's eye because it is more direct
than any regression: NHDPlus HR already carries `QAMA` (mean annual flow) and
`VAMA` (velocity for that flow), so continuity gives a cross-sectional AREA at
mean annual flow directly, with no regression at all. That is a measured-adjacent
quantity rather than a fitted one. It still needs a width and a shape assumption
to become a bed, so it does not escape the shape question, but it replaces the
weakest link in the regression chain with a published attribute.

What should NOT be built: a thalweg burn (section 2a, wrong data class by
construction), and a bare uniform-flow trapezoid without a conveyance check
(Neal et al. quantify its over-prediction bias, and the gradually-varied-flow
correction that removes it).

---

## 5. Verification status summary

Primary sources fetched and read this session: Bieger et al. 2015; Lindsay 2016;
Zheng et al. 2018; the ISPRS SDB accuracy paper; Heldmyer et al. 2022 HESS; the
VDatum REST API docs; the eHydro `SurveyJob` FeatureServer; the BlueTopo product
and registry pages; the NHDPlus HR attribute documentation; HEC-RAS boundary and
terrain-modification documentation; the CUDEM and NOAA coastal lidar S3
listings.

Explicitly UNVERIFIED, and not to be treated as load-bearing without a
re-verification pass:

1. NXSDB's exact ScienceBase download URL (403 or cert failure from this
   sandbox; existence, naming and scale corroborated independently).
2. BlueTopo's specific resolution tier values (the tile-index geopackage is the
   authority).
3. Whether NOS BAGs have a bulk REST API beyond the interactive viewer.
4. Whether USGS topobathy pilot projects are mirrored into `usgs-lidar-public`.
5. Whether an eHydro REST service exposes soundings directly rather than only
   survey footprints.
6. The exact `.cli` column ordering for a prescribed-elevation free-velocity row
   (the 4/5/6 code meanings are well corroborated; the ordering is exporter-
   dependent).
7. Thompson's parallel-mode availability in current TELEMAC releases.
8. FEMA's specific downstream-boundary recommendation language (PDF text
   extraction failed).
9. Any primary source naming the gauge-datum versus DEM-bed mismatch problem
   directly. Not found.
10. Any TELEMAC User Conference paper on synthetic bathymetry, or narrowly on
    downstream-boundary methodology. Not found.
11. The "BathyMet" method name from the charter. Not located in the literature
    searched.
12. Dey et al.'s specific peak-lateral-seepage error figure (surfaced only in a
    secondary summary, not confirmed against primary text).

Several fetch failures above (usace.army.mil, sciencebase.gov, opentelemac.org)
were TLS or 403 errors that look like a sandbox network or certificate issue
rather than the sources being unavailable, since the URLs are corroborated by
multiple independent citations. A re-run from a normal network would likely
clear items 1, 5 and the opentelemac.org items.
