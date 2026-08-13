# Test_WWM_Duck fixture -- SCHISM+WWM coupled-wave validation (Duck NC FRF, 12 Oct 1994)

The `schism_coupled_waves` archetype's baked published case (ADR 0126 / 0129): a
two-way wave-current coupled SCHISM+WWM hindcast of an energetic nor'easter over
the US Army Corps Field Research Facility (FRF) at Duck, North Carolina, 12 Oct
1994. Self-contained: 33586 elements / 17054 nodes (`hgrid.gr3`, local FRF
projection), nvrt=31 SZ, a 4-hour run (`rnday=0.16667`, dt=10 s) starting
1994-10-12 17:00 UTC, MSC=MDC=12 spectral bins.

## Provenance

Fetched from the SCHISM verification-tests SVN-over-HTTP mirror
(`https://columbia.vims.edu/schism/schism_verification_tests/Test_WWM_Duck/`) and
staged PRISTINE. SHA256 pins recorded in `SHA256SUMS` -- verify with
`sha256sum -c SHA256SUMS`.

- Wave boundary forcing SHIPS WITH THE CASE: `wwminput.nml` (IBOUNDFORMAT=6,
  LBCSP=T, FILEWAVE=DUCK94_wave_spectra_8m_array.nc) drives a bundled
  non-parametric spectrum from the 8m-array observations -- no WW3/parametric
  build.
- `param.nml` sets the FAITHFUL GOTM k-epsilon turbulence closure (`itur=3`,
  `mid='KE'`, `stab='KC'`) -- the reason the worker ships the
  `pschism_WWM_GOTM_TVD-VL` build variant.

## Published verification data (`Data/`)

- `timeseries_data_1010_to_1410_004Hz_025Hz.mat` -- the cross-shore pressure
  transducer transect: gauge positions (`xPTs`/`yPTs`) + measured spectral wave
  parameters (`Hm0_nlin` significant height, `Tp_nlin` peak period). The
  cross-shore Hs/Tp V&V compares the coupled model at these gauges (the acceptance
  chart).
- `12101994_sled_data.mat` -- sled 3D current profiles (secondary reference).

## Staging transforms (applied in code, `deck_authoring.stage_wwm_duck_deck`)

The pristine v-master deck carries three master-only namelist vars the pinned
v5.11.0 binary does not declare (`nbins_veg_vert`, `nmarsh_types`, `RADFLAG`);
staging strips them, copies `gotmturb.inp` -> `gotmturb.nml` (SCHISM's
`init_turbulence` hardcodes that name) and `hgrid.gr3` -> `hgrid_WWM.gr3` (WWM's
own grid file), and trims the output set to the postprocess targets
(`elevation` + `sigWaveHeight` + `peakPeriod`) so a small scribe count runs on a
modest core budget. `itur=3` is KEPT (the faithful coupled config).
