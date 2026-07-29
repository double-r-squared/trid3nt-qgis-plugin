# fetcher-fold replication-parity harness

The harness's job is now **spec-vs-live-twin at fold time only**: each phase-2
wave adds a SELF-CONTAINED `drivers_waveN.py` that grades the wave's promoted
sources (spec+router) against their still-present hand-written twins over the
same real endpoints, on the contract-4.2 edge matrix (`harness.py`).

Retired 2026-07-29 (ADR 0045 rider): the original `run.py` + `drivers.py`
twin-A/B machinery imported the wave-1/2 PILOT twins, which were DELETED at
promotion (ADR 0038/0039) -- so those imports no longer resolve and the offline
A/B is un-runnable for a folded source. The per-wave drivers (`drivers_wave3.py`,
`drivers_wave4.py`) + the `results/` verdicts stay as the historical record;
`harness.py` is the shared grading library they import.

- `harness.py` -- shared `SourceResult` / verdict gate / FGB+raster parsers / stub.
- `drivers_wave3.py` -- USGS water-data family (dataretrieval-delegated), `results/VERDICT_wave3.md`.
- `drivers_wave4.py` -- station family (CO-OPS currents snapshot), `results/VERDICT_wave4.md`.
- `results/VERDICT.md` -- frozen wave-1/2 record (pilots + ArcGIS family).

Run a wave gate (needs outbound network to the wave's endpoints; `read_through`
is stubbed, no MinIO): `python drivers_wave4.py`.
