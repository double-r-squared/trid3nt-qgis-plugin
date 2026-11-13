# ehydro

Recorded 2026-11-12 from the USACE eHydro survey ZIPs (the `sourcedatalocation` of each survey in the
`eHydro_Survey_Data` FeatureServer layer 0 index), nothing edited but the heads taken:

- `<id>.XML` - the package's own metadata member, read out of the ZIP with `zipfile.ZipFile(...).read(name)`.
- `<id>_SurveyPoint_head20.fgb` - the first 20 rows of the package's `SurveyPoint` feature class,
  `pyogrio.read_dataframe(gdb, layer="SurveyPoint", max_features=20)` written with `pyogrio.write_dataframe`
  (its CRS carried).
- `MO_11_OMM_20260415_CS_606_614_FULL_head300.XYZ` - `head -300` of the package's `_FULL.XYZ`.

Packages: MO_11_OMM_20260415_CS_606_614 (CENWO, blank datum, `_FULL.XYZ` stated NAVD88),
MO_10_OML_20260930_CS_517 (CENWO, blank datum, no elevation file stated), MO_01_LMR_20260902_CS_58_0_59_0_SORT
(CENWK, `mllw` against depths below the water surface), SP_01_SPH_20261015_CS_2026_180_HF and CB_23_CR3_20261110_CS
(tidal, MLLW in both places).
