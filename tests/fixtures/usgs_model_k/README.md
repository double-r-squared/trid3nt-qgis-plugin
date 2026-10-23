# USGS MODFLOW model archive excerpts

Offline excerpts of the two published ScienceBase MODFLOW model archives the
`fetch_usgs_model_k` and `fetch_usgs_model_layers` rows read, built from the
real downloads so the rows' parsers and pipeline see the same member paths, text formats and grid shapes
they see in production:

- `jbcc_ring_excerpt.zip` - Joint Base Cape Cod (JBCC) MODFLOW-2005 archive
  members `model/externalfiles/Ks/mmr_pp{n}._kx` (K, `%.18e` + CRLF, 290x400)
  and `model/externalfiles/Ibs/ib{n}` (`%5d` + CRLF) for layers 1-32, plus the
  layer surfaces `model/externalfiles/Bots/model_top_c2_3` (layer 1 top, feet
  on NGVD 29) and `Bots/bot{n}` (layer n bottom).
- `dm_ring_excerpt.zip` - Des Moines MF6 archive members
  `external_files/props/l{n}._kx` (K, `%.4e ` rows) and
  `external_files/id/l{n}_id.dat` (`%3d ` rows), 427x383, layers 1-6, plus the
  surfaces `external_files/surfs/l1_top.dat` and `l{n}_bot.dat` (metres on
  NAVD 88).

Cells inside each model's canary-ring floor/ceil window (JBCC rows 112:196,
cols 70:153; DM rows 239:290, cols 248:299) keep their REAL values; cells
outside are 0 with mask=0, so an AOI in the padded zone yields no active
cells (the honest empty path). All data is cell-indexed and cropped before
writing - no full-resolution raster layer is committed.

Sources (real archives, not committed):

- JBCC: ScienceBase item `631405ded34e36012efa3476`, DOI 10.5066/F77P8XCT (2019).
- Des Moines: ScienceBase item `6601d849d34e64ff1548e2ad`, CC0-1.0 (2024).

Round-trip verified: re-parsing these members through the row's D6 pipeline
reproduces the canary medians (JBCC L1 70.808, DM L1 25.037, L2 10.028,
L5 51.718, L3 empty) and probe cells (64.271 / 28.973) to 0.01 m/day.

The surface members were added to the same zips with the same window and
formats (JBCC `%12.2f` + CRLF, DM `%8.3f ` rows + CRLF).
