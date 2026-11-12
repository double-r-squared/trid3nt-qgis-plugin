# Des Moines MF6 calibration-target excerpt

`dm_targets_excerpt.zip` holds three members of the Des Moines River alluvial aquifer MF6 data release's
`ancillary.zip` (ScienceBase item `6601d849d34e64ff1548e2ad`, Davis and Bristow 2024,
https://doi.org/10.5066/P13ZDDVY, CC0-1.0), byte-identical to the archive's own and at the archive's own
member paths - the three tables `fetch_usgs_model_head_targets` reads:

- `pestfiles/tr_mf6_sim/input/pest_dmww_tr6.obs_data.csv` - the control file's observation table.
- `scripts/run_model/output/DMWW_gw_wl_obs.csv` - the GeoSam and wetland wells' targets with their bores.
- `scripts/run_model/output/DMWW_gw_nwis_wl_obs.csv` - the USGS wells' targets with their bores.

The whole archive (207961435 bytes, sha256 `4fe417f6b1188fe5438c5eb2a10c7addbca0e8ab7e46431daa6da26f62ba95a7`)
is not committed. The excerpt zip's sha256 is `9a20a30ebec76b195eead6d5fbd90c528e0fd8bbcfc47cce35531faf35028feb`.
