# Worker LOC ledger - the worker dissolution

Counting command: find workers/<dir> -name '*.py' | grep -v __pycache__ | xargs wc -l (physical lines);
tests counted separately (files matching test_*). Baseline row 0 taken at 07764c32~1 (= 3c7053e2)
BEFORE any dissolution wave. Target doctrine (IDEAS 2026-08-25): a worker is the
ENGINE ROOM - solver + glue on a staged run dir; fetchers/mesh builders/baked
values/fat tests all migrate or die. DoD = --network none.

## Row 0 - baseline (2026-08-25)

| worker dir | product LOC | test LOC | files |
|---|---|---|---|
| workers/elmfire/ | 1521 | 570 | 4 |
| workers/geoclaw/ | 3228 | 1568 | 6 |
| workers/_geoclaw_postprocess/ | 661 | 130 | 3 |
| workers/hecras/ | 900 | 417 | 8 |
| workers/hecras2025/ | 6852 | 1462 | 43 |
| workers/landlab/ | 3502 | 536 | 5 |
| workers/_landlab_postprocess/ | 322 | 100 | 3 |
| workers/mesh/ | 346 | 0 | 3 |
| workers/modflow/ | 7221 | 4075 | 16 |
| workers/_modflow_build/ | 181 | 0 | 2 |
| workers/_modflow_postprocess/ | 1844 | 0 | 2 |
| workers/openquake/ | 1722 | 371 | 5 |
| workers/_openquake_postprocess/ | 348 | 119 | 3 |
| workers/_raster_postprocess/ | 2031 | 592 | 11 |
| workers/schism/ | 544 | 159 | 6 |
| workers/sfincs/ | 720 | 116 | 3 |
| workers/_sfincs_build/ | 3342 | 210 | 6 |
| workers/swan/ | 1866 | 857 | 5 |
| workers/_swan_postprocess/ | 478 | 117 | 3 |
| workers/telemac/ | 9348 | 2383 | 25 |
| **TOTAL** | **46977** | **13782** | |

Per-wave rows append below with delta + running net + honest verdict, same
rules as docs/validation/skeleton-loc-ledger.md. The dissolution is DONE when
every worker dir holds solver glue + thin manifest/exit tests only and runs
--network none.
