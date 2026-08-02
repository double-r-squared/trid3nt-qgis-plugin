# 0080 - STAC multi-asset RGB composite: the imagery trio folded (landsat / sentinel2 / naip), one composite mode

Context: ADR 0078 named the STAC imagery class as a residual -- fetch_landsat_imagery,
fetch_sentinel2_truecolor and fetch_naip all bake a 3-band uint8 RGB COG from PC STAC,
which the single-band-float32 ``stac_float`` mode (modis_lst / mobi / copernicus_dem /
sentinel1_sar) cannot express. This wave builds ONE new raster-cog access mode --
``stac_multi_asset_rgb`` -- serving all three (and every Landsat band_combo, thermal LST
included), reads each twin end-to-end, proves value-identical pixels vs the twins (offline
synthetic + live PC STAC), promotes the three specs under the twin names, and deletes the
coded twins + tests.

Decision (2026-08-01):

## BUILT: the ``stac_multi_asset_rgb`` access mode (ADR 0080)

A new raster-cog access mode (a distinct ``ingest.access``; STRICT no-op for the 4 prior
``stac_float`` specs -- they never select it). Per STAC item it:

- reads N single-band RGB assets (or N bands of ONE multi-band asset) + an optional QA/SCL
  mask asset, windowed-reprojected to the AOI grid through the shared transport opener;
- applies a per-combo ``transform`` (``reflectance`` DN->reflectance with fill->NaN;
  ``lst_celsius`` DN->deg C; or ``none`` for raw uint16 / uint8);
- builds a bad-pixel mask (``bitmask`` bit-test over qa_pixel, or ``classes`` set-membership
  over SCL);
- bakes one of three renders: ``joint_stretch`` (a joint 2/98-percentile cross-band stretch
  to RGB), ``colormap`` (a percentile-stretched single band through a matplotlib inferno
  ramp -- landsat thermal LST), or ``passthrough`` (raw uint8 straight to RGB -- naip);
- serializes a 3-band photometric-RGB DEFLATE COG (``_rgb_cog_bytes``, byte-identical to the
  twins' ``_write_rgb_cog``; the multiband-passthrough token renders it directly).

Scene selection is a declarative ``stac.select`` block: an ``eo:cloud_cover`` query filter,
an optional platform filter (landsat 8/9, widened to 4/5/7 by ``include_legacy_landsat``),
and an ordered ``rank`` of sort keys -- ``coverage_bucket`` (full-coverage scenes first) ->
``cloud_cover`` (least cloudy) -> ``coverage`` (most overlap) for landsat; ``cloud_cover``
alone for sentinel2; empty (most-recent-intersecting) for naip. The coverage-fraction leg
is the shared ``_aoi_coverage`` helper the ADR 0079 sentinel1 ``coverage`` select was
refactored onto (one shared function, two callers).

Two minimal no-op contract/router extensions support it:
- ``ParamSpec`` enum-``aliases``: an enum param maps a known alias to the canonical value
  before the allowed-set check (landsat ``band_combo`` accepts rgb/natural/cir/lst/...),
  echoing the canonical key; an unknown value still raises the typed BAND_COMBO_INVALID.
  No prior enum param declares aliases (strict no-op).
- ``OutputSpec.role_by_param``: MAP a param value to the LayerURI ``role`` (landsat thermal
  LST -> ``primary``, the RGB composites -> ``context``), a value absent from the map falls
  back to the static role. Mirrors ``style_preset_by_param`` / ``units_by_param``. No prior
  spec declares it (strict no-op).

## FOLDED: the three sources (spec + no-op recipe on the one mode)

- **fetch_landsat_imagery** -- band_combo-keyed recipes: ``true_color`` (red/green/blue),
  ``false_color_nir`` (nir08/red/green) both reflectance-scaled + qa_pixel bitmask +
  joint-stretch (span_floor 1e-6); ``thermal`` (lwir11) lst_celsius + qa_pixel + inferno
  colormap. role_by_param thermal->primary; units_by_param thermal->"Land-surface
  temperature (deg C)". platform_query (8/9 +legacy 4/5/7) + coverage/cloud rank.
- **fetch_sentinel2_truecolor** -- B04/B03/B02 raw uint16 (transform ``none``) + SCL classes
  mask + joint-stretch (span_floor 1.0, ``nodata_rule: all_bands_zero`` reproducing the
  twin's r==0 & g==0 & b==0 nodata). least-cloudy rank.
- **fetch_naip** -- the SMALLEST directive: ONE ``image`` asset, bands 1..3, ``passthrough``
  render (no scale/mask/stretch), most-recent-intersecting item, all-black -> NO_COVERAGE.
  px_max 8192 (naip's sub-metre cap). CONSUMER: compute_canopy_height imported the twin
  symbol directly -> re-pointed to the registry seam (``TOOL_REGISTRY["fetch_naip"].fn``),
  test_compute_canopy_height re-pointed + green.

Metadata flags pinned TWIN-IDENTICAL before deletion (SPEC-IDENTITY rule): supports_global_
query=false, cacheable=true (ttl_class static-30d), auto_publish=true, emit_bbox=false
(all three twins omit LayerURI.bbox). error_prefix LANDSAT / S2_TRUECOLOR / NAIP; empty
suffix NO_IMAGERY / NO_IMAGERY / NO_COVERAGE; bbox+band_combo per-param BBOX_INVALID /
BAND_COMBO_INVALID -- byte-identical A.6 codes.

## Live + offline evidence

- **OFFLINE synthetic parity PASS** (pre-promotion): twin ``_fetch_*_cog_bytes`` vs router
  ``execute`` over synthetic source COGs (cloud/nodata bands, fill DN) -> PIXEL-IDENTICAL
  for s2_truecolor, naip, and landsat true_color / false_color_nir / thermal.
- **LIVE PC-STAC parity PASS** (real Azure blob reads, same AOI + pinned window): naip
  (2226x1872, range [1,249]), s2_truecolor (223x193, [0,255]), landsat true_color /
  false_color_nir / thermal (223x193) -- every source VALUE-IDENTICAL to its twin,
  ``np.array_equal`` over the decoded RGB arrays.
- Offline suite FAILED set unchanged from the 9-failure baseline (the mode + the two enum-
  alias / role_by_param extensions are strict no-op for every prior spec). Retrieval
  UNSHIFTED (24/24 corpus phrasings across the three tools rank top-8, model-free).

## Non-gating divergences

- **cache key**: the router keys on the validated request params (bbox / start_date /
  end_date / band_combo / max_cloud_cover / include_legacy_landsat) rather than the twin's
  computed datetime_range string + platforms list; a DEFAULT-window request no longer keys
  on today's date (stable cache across days -- the stac_float ``latest`` precedent). The
  cache source_class prefix stays the twin's (cache indistinguishability preserved).
- **layer_id / name synthesized** by the router (the twin's per-combo name / f-string
  layer_id are not reproduced) -- the standing fold divergence; role / units / style_preset
  are reproduced exactly (role_by_param / units_by_param / the constant landsat_rgb token).
- **band_combo aliases**: reproduced verbatim via the new enum-alias table (the twin's
  friendly-alias map); an unknown combo raises the identical LANDSAT_BAND_COMBO_INVALID.

## Metrics

Coded fetchers -3; coded tools -3. Spec-served sources 67 -> 70 (+3). Registry name-count
UNCHANGED (three coded twins died, three spec surfaces took their names). test_catalog_
surfacing: n_specs 67 -> 70, arm declarable delta -66 -> -69, stratum index 66 -> 69
(expected promote metrics). Offline baseline UNCHANGED (FAILED set == the 9 pre-existing).

Consequence: the raster-cog family gains a ``stac_multi_asset_rgb`` mode (multi-asset RGB /
LST-colormap / uint8-passthrough composites from PC STAC with a cloud/coverage scene rank),
opt-in-no-op for the prior stac_float specs; the enum-alias + role_by_param no-op extensions
land alongside it. Three ADR 0078 STAC-class fold-ready verdicts close. Supersedes nothing;
extends the tier-3 hook contract (ADR 0056/.../0079) and the stac raster mode family. The
GLM/GOES/animation frames-list residuals (ADR 0078) and the netcdf_cf_object goes_satellite
mode remain STOP-RULED.
