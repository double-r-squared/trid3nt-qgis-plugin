# Reanalyze ledger

Decisions taken deliberately that NATE wants to RE-ANALYZE later - not
parked work (that lives in IDEAS), not deletions (DELETION_LEDGER):
choices that stand today with a stated trigger for re-examination.
Convention: one entry per decision - what was decided, why, the
evidence at decision time, and the REVISIT TRIGGER.

## 2026-09-03 - rain-on-grid outlet: startup transient accepted
DECIDED: the derived Z(Q) rating-curve outlet pins WITH a measured
startup transient - 26/60 listing samples show flux briefly entering
the outlet before runoff begins (max 2.31 m3/s, ~20,285 m3 = 0.46% of
the storm), visible as a small negative trough in the delivered
hydrograph. The "all-exiting" acceptance phrase was orchestrator
wording stricter than the physics; a wetting boundary under an
imposed level can legitimately pass a bounded transient inflow, and
inventing an activation threshold would be sad-path machinery
(happy-path law). Evidence: run 01M1N1YP436AY5MQFER74BV7SN;
continuity -2.4e-15; water balance closed to the m3.
REVISIT TRIGGER: (a) the calibration era's gauged rating curves land
(the derived curve is replaced - re-measure the transient); (b) any
case where the trough measurably distorts a delivered hydrograph
answer; (c) the spin-up item lands (a settled initial state may
remove the dry-catchment startup class entirely).

## 2026-09-04 - uri_registry: the handle indirection survives the transport collapse
DECIDED: the spec's dies-row reads "uri_registry translation layers
(~1,200 -> thin id-to-URI record)". Everything that TRANSLATES was cut
(the wms/tile display face, the gs:// scheme, both display-face
resolution branches), landing 1,205 -> 1,035. What survives is the
layer-handle indirection: the L<n> mint the model is shown, the emit
rewrite that hands it those handles instead of uris, the fuzzy
mangle-match and the placeholder resolution. Those resolve an ID, not
a scheme, and cutting them would revive the URI-hallucination class
the module was built to prevent - so the row was met at the layer it
names rather than by LOC, and the residual is reported instead of
taken. Evidence: docs/validation/emission-fold-store-conformance.md,
deviation 1.
REVISIT TRIGGER: (a) the emit rewrite is proven to leave NO path by
which a model can echo a raw store uri into a *_uri param - then the
fuzzy match and the placeholder branch are dead weight, not defense;
(b) a wave that changes what the model is shown for a layer (a
different handle vocabulary) - the indirection is re-derived, not
patched; (c) NATE reads the residual and rules the thin record is what
he meant.

## 2026-09-04 - sheet form-card default view: set slots + open mandatory, rest under advanced
DECIDED: the module-surface sheet's card shows what the template or a
fill set (with provenance) plus any mandatory slot still open; every
other slot sits under advanced, grouped by the dico's rubrique, greyed
with its engine default. Chosen over "level-0 slots always visible"
(the dico's ~220 core keywords for telemac2d) for card length.
Evidence: 376 telemac2d keywords, 220 at NIVEAU 0 (measured in-image).
REVISIT TRIGGER: NATE's standing intent - a SIDE-BY-SIDE evaluation of
the two views once the sheet is live, judged on which performs better
for a human and for the model filling a sheet; the winner replaces the
default. Backburner until fill/run has real use.

## 2026-09-06 - bed datum: one stated datum per source, refuse otherwise
DECIDED: a bed source states its vertical datum on its source row;
set_bed harmonizes to the run's datum or refuses by name listing the
populations it measured; a mosaic mixing datums is not a bed source
for a water domain. General - no water-body vocabulary, no new ladder
class. Evidence: Marquette Lower Harbor basin bed off the NCEI mosaic:
778 nodes at -8.91..-0.10 m (lake datum) and 49 shoreline nodes at
+178..+194 m (NAVD88 land tiles), the guilty node of the failed 3D
tracer step inside the 49; the fetcher stated no datum.
REVISIT TRIGGER: (a) the coverage-map bed subsystem lands (all
recorded bathymetry indexed, the source an AOI lands inside served) -
the datum rule becomes a property of the index, and stitching two
stated-datum sources with their acquisition times becomes a user
tool; (b) a supported AOI where the only bed source cannot state its
datum - decide per case whether a stated constant offset is honest;
(c) NATE's posture that the user refines the bed visually needs the
bed layer's provenance card to show datum + acquisition time per
source - if that card is missing, this rule is the only guard.
RATIONALE (NATE 2026-09-06): DEM and satellite products are full-
coverage for any AOI, so a fetcher can promise them; bathymetry is a
patchwork of surveys, so the honest first question is "does anything
cover here, and which" - an INDEX problem (a coverage map, ours or an
existing tool), not a fetcher problem. For now ONE bed source is
plugged in per template; the coverage-map concept is revisited once
the module-surface wave completes.

## 2026-09-06 - the two-populations bed guard deleted
DECIDED: `_refuse_two_datums` (the post-paint heuristic that cut the
sorted bed at its largest gap and refused "two clouds") is DELETED
with its test. The stated vertical datum on every bed source row is
the guard: a raster mixing datums cannot reach the bed step because
no registered source states two. Evidence: the heuristic refused the
Scotia reach on ONE node at 44.95 m against 906 at 13.00-26.30 m
(real high ground the mesh touched), blocking river_oil_spill,
river_scour and river_sediment_plume; the same reach at 14 m edge
passed - resolution-dependent. It guessed about terrain and guessed
wrong; with the datum rule landed it had no remaining job.
REVISIT TRIGGER: (a) a bed source that states one datum but is
measured to carry two (a stitched product whose metadata lies) - then
a population test returns as a MEASURED check with a minimum cloud
size and a stated gap-to-spread ratio, never as a guess; (b) the
coverage-map bed subsystem lands and stitches sources - the seam
between two sources is exactly where a cloud test would fire, so the
stitch must carry the datum per source, not re-derive it.

## 2026-09-09 - STAC rasters read on GDAL's HTTP, two norm clauses traded
DECIDED: the nine catalog-published raster rows (`stac_raster.py`) read
their assets through GDAL's own HTTP client, configured once via
`odc.loader.configure_rio(GDAL_HTTP_MAX_RETRY=5, RETRY_DELAY=1,
RETRY_CODES="429,500,502,503,504")`, rather than through
`_router/transport/`. `odc.stac.load`'s reader is closed
(`odc/loader/_rio.py:601` is a bare `rasterio.open`, no `opener=`), so
this is the price of the library owning search, grid and fuse.

Two clauses of the upstream-error norm are TRADED, both measured at
Stage 0 (docs/validation/fetcher-fold-stage0.md, Probe A):
(a) the upstream STATUS is verbatim (`RasterioIOError: HTTP response
code: 404 / 403 / 409`, proven on real objects) but the S3 XML
`<Code>NoSuchKey</Code>` BODY never reaches Python -
`CPL_CURL_VERBOSE=YES` recovers zero body tokens in 96/105 log records
and costs a ~100-record-per-read firehose; `transport/opener.py`'s
`<Code>` recovery has no GDAL equivalent;
(b) `Retry-After` is NOT honored - source-confirmed at
`port/cpl_http.cpp` v3.12.4 `CPLHTTPGetNewRetryDelay`, which is
exponential-with-jitter and reads no header. Measured on a local
origin sending `Retry-After: 6`: gaps 1.00 s then 2.42 s, identical
with the header absent. Retries DO fire and DO recover (429 and 503,
two failures then 200).
Blast radius: nine rows, all raster, all read-only. The other twelve
transport-bound raster rows (F6) and every vector row keep
`transport/client.py`, where `Retry-After` is honored at block
granularity.
REVISIT TRIGGER: (a) odc-stac exposes a reader seam (an `opener=`
pass-through or a supported `ReaderDriverSpec` for the rio driver) -
then these rows return to our transport and both clauses are met;
(b) GDAL's `CPLHTTPGetNewRetryDelay` learns `Retry-After` (watch the
cpl_http changelog); (c) a real 429 from Planetary Computer or
Earthdata that the exponential backoff fails to clear - then the wait
policy is the thing that has to change, not the reader.

## 2026-09-09 - vectors read on GDAL's drivers, and esri-json is not geojson
DECIDED: the ESRI `/query`, OGC API - Features and zipped-shapefile
rows (`vector_ogr.py`) read through GDAL's own vector drivers via
pyogrio, not through `_router/transport/`. pyogrio links its OWN
libgdal (measured: inside `rasterio.Env(GDAL_HTTP_MAX_RETRY='99')`
rasterio sees 99 while pyogrio sees 4), so the read policy is applied
with `pyogrio.set_gdal_config_options` and is a second config store,
not the raster family's.

The same two clauses the STAC family trades are traded here, measured
at Stage 0 Probe B on GDAL 3.12.4 (docs/validation/fetcher-fold-
stage0.md): the upstream STATUS is verbatim
(`pyogrio.errors.DataSourceError: HTTP response code: 404 / 403`) but
the S3 XML `<Code>` body is not, and `Retry-After` is not honored
(identical gaps 1.00 s / 2.42 s with and without the header). A THIRD
loss is NOT accepted: an ArcGIS service answers a rejected query
200-with-`{"error": ...}`, which the driver can only report as
`Invalid FeatureCollection object. Missing 'features' member.`;
`vector_ogr._verbatim_upstream` re-reads that one URL through the
transport and surfaces the service's own message, so the honesty floor
holds where the driver drops it.

MEASURED, and the reason the ESRI rows are not byte-identical to their
pre-fold artifacts: the driver reads `f=json`, the hooks read
`f=geojson`, and ArcGIS's GeoJSON writer is not a lossless view of the
same rows. On `fetch_nwi_wetlands` over a Naples, FL bbox - 2,683
features both ways, same vertex count per feature, identical values in
all three columns, union bounds equal to the last digit - the two
differences are (a) coordinate precision: esri-json carries full
float64, the GeoJSON writer rounds, max Hausdorff distance
7.05e-10 deg (0.078 mm); (b) exterior ring winding: the GeoJSON writer
rewinds to RFC 7946 counter-clockwise, esri rings arrive in the
service's own clockwise order, so all 2,683 exteriors flip. Neither is
corrected in the fold: the driver's read is the closer of the two to
what the service holds, and no consumer of a FlatGeobuf depends on
ring orientation. Stated here so a later pass reads a winding flip as
this decision rather than as a regression.

Two more measured consequences of reading `f=json`, both the driver
honoring what the service DECLARES where the geojson round trip lost
it. (a) FIELD TYPES: a column the layer declares
esriFieldTypeDouble arrives float64 rather than the int64 pandas
inferred from a whole JSON number (VOLTAGE, ACRES, SYSTEM_ID,
LEVEED_ID, attr_IncidentSize, attr_PercentContained), and a column the
layer declares esriFieldTypeDate arrives as a datetime rather than as
epoch milliseconds (SOURCEDATE, VAL_DATE, poly_DateCurrent). The one
column_map rule that read epoch milliseconds is now `date_iso` and
takes either. (b) POLYGON HOLES, and this one is a correctness win
rather than a representation change: esri rings carry the hole in
their winding, and ArcGIS's GeoJSON writer drops it. Measured against
the live service on `fetch_us_drought_monitor` over a Texas bbox - the
esri-json feature has 1,219 rings of which 56 are counter-clockwise
(holes), the geojson feature has 1,219 outer polygons and zero holes -
so the pre-fold layer painted 56 donut holes as filled drought and
over-counted its own area by 16 percent (223.85 vs 193.11 square
degrees on that one feature). The driver organizes them; every
polygon row in this family was carrying the same error.
