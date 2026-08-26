# 0317 - The fetch migration: the open-water bed leaves the container

## Context

Four of the seven TELEMAC families - the coastal strip, the lake fetch, the
harbour basin and the 3D column - fetched the bed they solve on from inside the
solver container. The same twenty lines against the NOAA NCEI `DEM_all`
ImageServer, copy-pasted four times, single-shot, no retry, no mirror. Outside
emit-on-fetch, outside the cache, outside provenance, outside the declared
fallback ladders, and outside the F-arc audit's denominator, which swept
`trid3nt_server/` only.

The external-fetch audit graded that class and the worker doctrine named the
end state: a worker is the ENGINE ROOM - solver plus glue on a fully staged run
directory, no network, no defaults, no opinions - and the portability test asks
of every line, "would this change if the box moved?" A bed fetch changes if the
box moves. It is server tier.

The staging machinery to fix it already existed and was deliberately empty:
`stage_open_water_manifest` wrote `"inputs": []` with a comment naming the
bypass, while `launch_local_solver` had walked `{gs_uri, dest}` into the rundir
for every other engine since SFINCS.

## Decision

**The bed is a declared `Data` producer.** Each of the four templates declares
`Data("bed", Fetch.tool(".../open_water.fetch_domain_bed", ...))`; the producer
routes a new `fetch_ncei_dem_mosaic` spec, the deck writer turns the result into
one manifest `inputs` row, and the worker opens a file.

**The spec reproduces the request, not an approximation of it.** The four
builders asked for three different sample lattices (1200, 1800 and 3000 px per
DEGREE, with three different per-axis caps), and an angular lattice is not a
metric cell size. Rather than re-grid and accept drift, the ImageServer executor
gained a declared `px_per_deg` sizing alongside its `native_cell_m` one, and the
lattice travels from the template as a param. The bytes the router caches are
byte-identical to the bytes the worker used to fetch - verified by sha256 on the
coastal canary's own bbox before anything was migrated.

**`--network none` is a per-spec declaration, applied by the launcher.**
`LocalSolverSpec` gained a `network` field; `launch_local_solver` inserts the
flag. It is per-spec ON PURPOSE: an engine whose in-container fetches have not
migrated would fail under a global switch, so each adopts the posture as its own
inputs become staged. Five hand-copied TELEMAC `build_argv` closures collapsed
into one factory in the same edit, because a posture spread across five identical
closures is one that drifts.

**The node-lattice bed COG dies.** It existed only because a container fetch
could not reach the emit seam. The staged source raster is continuous and IS the
data the nodes were sampled from, and emit-on-fetch surfaces it for free.

**ARTEMIS's schematic breakwater dies with it.** The deck is the only authority
on a structure; an unfilled slot is genuinely open water.

**One object store, one client.** `solver._get_s3_client` is canonical (it is the
only injectable one and ~70 modules already import it); ten inline
`boto3.client("s3", ...)` constructions and two duplicate URI splitters were
migrated onto it. `tools/fetchers/_public_s3.py` stays separate and is not a
duplicate: an UNSIGNED client against real AWS for third-party open data is a
different store with a different auth posture.

**A run records WHICH CODE made it.** `code_provenance.py` stamps the tree's sha
plus a dirty flag at dispatch (into `code_provenance.json` BESIDE the manifest,
never inside it - `manifest.json` is the worker's input contract and several
entrypoints gate it strictly), carries it into `completion.json`, and gives
readers `staleness()`, which lists the commits touching that engine's declared
paths since. The proof packet carries it as `code_staleness`; the diagnostics
envelope folds it into `warnings[]`. An unanswerable question returns a warning
that SAYS it is unanswerable rather than silence, which would read as a clean
bill of health.

## Consequences

Parity, run for run through the rebuilt no-network image: coastal 19/19 composer
metrics and every worker metric identical (datum offset -0.232 m, peak WL
3.4863 m); tomawac identical; telemac3d identical; river_dye refined identical;
rain_on_grid identical; artemis resonance identical. ARTEMIS agitation moved, by
design - the recorded evidence was of the fabricated barrier.

Worker LOC: `workers/telemac/` product 9318 -> 9132, test 2383 -> 2336.

Three things this ADR deliberately does NOT do, each with its reason:

**The reach family keeps its six fetches.** `telemac_river_dye_build.py` still
navigates NLDI, re-seeds off two NHDPlus_HR flowline queries, queries NHDArea for
banks, and walks a PRIVATE Copernicus-STAC -> 3DEP DEM ladder. Two things block
it. The producer question is NATE's: the canvas today shows an OSM
`fetch_river_geometry` layer while the mesh is built on an NLDI centerline the
user never sees, and repairing that false surface by making the declared layer
the consumed one CHANGES THE SEED and therefore the physics. And the DEM rung is
not a like-for-like swap: measured on the Eel River canary reach, the router's
`fetch_dem(source="copernicus")` mosaic differs from the worker's own
`/vsicurl` sample of the same GLO-30 tiles by RMS 3.87 m and up to 22.3 m on
valley walls (mean 0.002 m, so a robust along-channel fit may well survive it -
but "may well" is not what a 16-significant-digit canary is compared against).
The parity-exact route is a native-grid mosaic that does not re-grid, which is an
executor change with its own risk. Until both are settled the reach family cannot
declare `network="none"`.

**The compute-class vocabulary keeps two spellings.** `medium` is a retained
synonym of the contract's `standard`. Collapsing it is 84 occurrences across 42
files, most of them model-facing `Param` defaults and template declarations whose
provenance rows and recorded canary args would all move - a fleet-wide rename, not
a solver-file tidy. What landed instead: ONE definition (`COMPUTE_CLASS_ALIAS`,
exported), which everything that validates a compute class now reads.

**Same-source reuse is designed, not built.** The rule - a ladder that resolves to
the same source and resolution as a raster already fetched for a covering window
clips rather than refetches - belongs in the router, whose wrong answer is wrong
DATA. The case that motivated it turned out not to be one: see below.

## The Coweeta 3DEP finding, which overturns its own premise

The premise was that `fetch_dem`'s 3DEP rung fell back at a US lidar heartland.
It does not. A live probe of the Coweeta bbox returns 3DEP at 10 m in 10.3 s and
at 30 m in 3.1 s. The Copernicus layer on that canvas is not a fallback at all:
`delineate_watershed` fetches its own DEM through
`_hydrology_common._stage_dem`, which is hard-wired to Copernicus GLO-30 because
D8 routing needs a natively GEOGRAPHIC grid - a constraint of the METHOD, already
documented, and NOT the same source as the 3DEP bare-earth mesh bed beside it. So
there is nothing to reuse: two DEMs, two datasets, two purposes.

What was genuinely wrong was that nothing said so. The layer read "Input:
copernicus dem (copernicus_dem)" next to "Input: mesh bed (dem, ...)" and the
user was told nothing about why there were two. It now reads "Input: D8 flow
routing (geographic grid); not the model bed". And `resolve_bed_dem`'s
cross-dataset fallback, which swallowed its own trigger into a log line, now
names it: "3DEP FAILED: <error_code> (<exception>) -> Copernicus GLO-30", carried
on the artifact as `fallback_reason` where every consumer reads it. A transient
timeout and a real coverage hole used to be indistinguishable in the record.

## A flake this wave found and did not cause

`telemac_do_sag_refined` is non-deterministic. Run 1 produced a different
centerline (sag curve sampled every 7.4 m instead of 17.6 m, BOD zero at every
station, DO minimum 8.9964 at 692.1 m); run 2, with no code change between them,
reproduced the recorded pin exactly (9.0081 at 123.5 m). The cause is almost
certainly the in-worker seed ladder: `_mainstem_flowline_seed` is FAIL-OPEN, so a
slow NHDPlus_HR query silently keeps the raw position seed and meshes a different
reach, with nothing anywhere recording which happened. That is the private-ladder
class this ADR migrates away from, and it is the strongest argument for finishing
the reach family.
