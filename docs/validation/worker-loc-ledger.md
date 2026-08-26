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
| workers/telemac/ | 9318 | 2383 | 25 |
| **TOTAL** | **46947** | **13782** | |

ROW-0 CORRECTION (2026-08-26). The whole table was re-run from git at the baseline
ref rather than from the published prose. Every product cell, every test cell and
every file count reproduces EXCEPT `workers/telemac/`, published as 9348 and
measured at **9318**; the TOTAL therefore moves 46977 -> **46947**. Two counting
facts, so the table can be reproduced exactly: the `files` column counts ALL `.py`
files in the dir (product plus tests), while the two LOC columns split them on a
`test_*` basename; and `workers/conftest.py` (34 product lines) sits at the workers
ROOT, inside no dir row, so it is in neither the rows nor the TOTAL. The command
behind every cell, run per dir:

    ref=3c7053e2
    for f in $(git ls-tree -r --name-only $ref workers/<dir>/ | grep '\.py$'); do \
      printf "%s %s\n" "$(git show $ref:$f | wc -l)" "$f"; done

Per-wave rows append below with delta + running net + honest verdict, same
rules as docs/validation/skeleton-loc-ledger.md. The dissolution is DONE when
every worker dir holds solver glue + thin manifest/exit tests only and runs
--network none.

## Per-wave rows

| wave | range | dirs touched | product delta | test delta | running net | verdict |
|---|---|---|---|---|---|---|
| B - TELEMAC coastal origin (`075ad814`) | `3c7053e2..02acbfed` | workers/telemac/ | +55 / -9 = **+46** | 0 | **+46** | the wrong direction, and unarguable: the SELAFIN header had no X/Y-ORIGIN, so the coastal result mesh landed off the bay. 46 lines to make three build scripts echo the corner they actually meshed. Nothing dissolved; the dissolution has not started. |

Counting note, because two spans are in circulation and they are not the same
measurement. The row above is `git diff --numstat 3c7053e2 02acbfed -- 'workers/'`,
which totals **+55 / -9 = +46** and is entirely product `.py` (three files in
`workers/telemac/`, all from commit `075ad814`; no test file changed). The other
figure that gets quoted, **+607 / -55 = +552**, is `git diff --numstat 0f7a6351
02acbfed -- 'workers/'` - and `0f7a6351` (2026-08-25 02:10) PREDATES this ledger's
own row-0 baseline `3c7053e2` (2026-08-25 22:56). That wider span re-counts work
already sitting INSIDE the baseline: `telemac3d_build.py` +258/-32, `artemis_build.py`
and `tomawac_build.py` changes, and a 219-line new test
(`workers/telemac/tests/test_telemac3d_vertical_grid.py`), which on this ledger's
product/test split reads +388 / -55 = +333 product and +219 test. Both numbers are
true of their own range. This ledger's stated rule is deltas from ROW 0, so the
number that belongs in the column is **+46**, and `0f7a6351..02acbfed` is recorded
here only so a reader who meets +607 / -55 elsewhere knows what it measures.

| C - the open-water fetch migration (`53591921`) | `02acbfed..<wave head>` | workers/telemac/ | +146 / -378 = **-232** | +2 / -49 = **-47** | **-233** | the dissolution starts. Four builders' copies of one HTTP fetch against the NOAA NCEI mosaic (~20 lines each) became one 55-line staged-raster reader; `_bed_cog.py` (109) and its two tests died with the node-lattice bed COG the server now publishes from the source raster instead; ARTEMIS's schematic-breakwater branch went with them. The image runs the four open-water families with `--network none`. |

Reading the two numbers in that row: **-279** is this wave's own span
(`f609b762..<head>`, `git diff --numstat -- workers/`), and **-233** is the running
net from row 0, which still carries wave B's +46. Per dir, against row 0:
`workers/telemac/` 9318 -> **9132** product and 2383 -> **2336** test; every other
worker dir is untouched, so the TOTAL moves 46947 -> **46714**.

The dissolution is NOT done for TELEMAC. `telemac_river_dye_build.py` still holds
six network fetches (NLDI snap + navigate, two NHDPlus_HR flowline re-seeds, the
NHDArea bank query, and the private Copernicus-STAC -> 3DEP DEM ladder), so the
reach family cannot run `--network none` yet. Why that half was left standing, and
what it costs to finish, is in ADR 0317.
