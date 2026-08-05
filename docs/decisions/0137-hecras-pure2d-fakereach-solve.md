# ADR 0137 -- HEC-RAS: the version-correct pure-2D reference is FOUND (Chippewa, dam-free) and the fresh carved topology SOLVES through a genuine pure-2D fake-reach deck; the 2D-BC-line Event-Conditions schema is the precise remaining STOP (OI-FT1 refined)

Status: accepted (2026-08-05)
Follows: ADR 0136 (the fresh-topology solve -- a repo-authored 2D tessellation is
accepted + solved by the 6.6 engines at vol err 0.0021%, but stays DRY; OI-FT1 =
directing an inflow onto the fresh 2D BC line, walled on "no combined-1D/2D `.bNN`
reference"; the x09 dam-entanglement finding), ADR 0135/0134 (the `b06`/BaldEagle
pure-2D reference + the "bare `Upstream Flow Hydrograph` == 2D BC line" claim),
ADR 0133 (the geometry writer), ADR 0132 (the Muncie transplant), ADR 0125 (the
SHA-pinned `Example_Projects_6_6` pin `ea239b50...386887`).

This wave runs THE WETTING REFERENCE HUNT: it sweeps the ENTIRE example
distribution for shipped Linux intermediates (ADR 0134 characterised ONE project),
FINDS the version-correct clean pure-2D reference the prior ADRs lacked, uses it to
make the fresh carved topology SOLVE through a genuine dam-free pure-2D deck, and
STOPS precisely at the one unshipped schema -- per the ADR 0133/0136 no-blind-
authoring charter.

Experiment-only-plus: NO server / tool / contract / registry change (registry
byte-identical by construction). New durable code + vendored public-domain
reference under `services/workers/hecras2025/subst/crux/{freshtopo,chippewa_reference}/`;
transcripts + solve logs under `scratchpad/wetting_proofs/`.

## The sweep (every shipped Linux intermediate, not one project)

The zip re-downloaded + SHA-verified EXACT against the pin (432389121 B). The full
sweep: **29 `.bNN` + 26 `.xNN`**, zero `.tmp.hdf`. Single-2D-area projects that ship
Linux intermediates: `BaldEagleCrkMulti2D` (`b06`/`x09`), **`Chippewa_2D`**
(`b02`/`x01`), **`Weise_2D`** (`b01`/`x08`), `DavisStormSystem` (Pipes, `b02`/`x02`).

## The decoded reference: the `.bNN` 2D-BC-line form is VERSION-DEPENDENT

The prior ADRs built on `b06`'s **BARE** `Upstream Flow Hydrograph`. The sweep shows
that is a **HEC-RAS 6.2 artifact**. In every 6.4 project (Chippewa, Weise, Davis)
the 2D-BC-line inflow is the **SUFFIXED fake-reach** header:

    Upstream Flow Hydrograph - River: Fake River  Reach: Fake Reach  RS: 100

The `.bNN` "Hydrograph Data" flag lines are byte-identical between a 1D reach inflow
(Muncie `b04`) and a 2D-BC-line inflow (Chippewa `b02`); the ONLY `.bNN` difference
is the header SUFFIX -- a REAL `River:/Reach:/RS:` (1D) vs the literal `Fake River /
Fake Reach / RS: 100` (2D BC line). **This is the root cause of the ADR 0136 STOP:**
it used the 6.2 bare form against the **6.6** engine, where a bare header maps
positionally to the 1D reach (exactly what ADR 0136 observed). The 6.6-correct
2D-BC-line header is the fake-reach form. ADR 0134/0135's "bare == 2D BC line" was
right for 6.2, wrong for 6.6.

## The x09 dam-blocker DISSOLVES -- Chippewa is the clean reference

ADR 0136 found the shipped pure-2D `x09` could not be reduced to a dam-free reach
(its fake reach's node numbering is entangled with the Sayers-Dam SA connection).
**`Chippewa_2D.x01` is that dam-free reference, shipped and valid-by-construction:**
a single 2D area as an SA, an **EMPTY** Storage-Area-Connection section (no dam, no
gate, no lateral weir), and a minimal `Fake River`/`Fake Reach` (2 dummy XS). Its
Arrays-Sizes A/B rows (`1 1 0 2 8 F` / `3 3 0 0 0 0 0 0`) are **byte-identical to
Weise's** despite a different mesh -- proving those rows encode the fake reach, NOT
the 2D cell/perimeter count, so a name+perimeter graft is Arrays-Sizes-safe (the
consistency the `x09` graft lacked). Vendored (public domain, CRLF-stripped, ~10 KB)
under `chippewa_reference/` with a decoded README + the `g01.hdf` BC-lines schema.

## The fresh carved topology SOLVES through a GENUINE pure-2D deck (the advance)

`build_chippewa_fakereach_deck.py` replaces ADR 0136's real White-River 1D reach
with the Chippewa clean fake reach (`patch_chippewa_xnn` / `patch_chippewa_bnn`),
over the SAME fresh carved NW-quadrant mesh (2068 real cells, 171-pt fresh
perimeter, 66 cut faces). It SOLVES end-to-end through production 6.6
`RasGeomPreprocess` + `RasUnsteady`:

| run | RasGeomPreprocess | RasUnsteady | vol err % | flux in/out | 2D wet cells |
| --- | --- | --- | --- | --- | --- |
| ADR 0136 (White-River reach) | Finished | Finished | 0.002150 | 36674/35305 | 0 (dry) |
| **this (clean fake reach)** | **Finished** | **Finished** | **0.000000** | **6743.8/6743.8** | **0 (dry)** |

The fresh tessellation is now proven to solve not just alongside a 1D reach (ADR
0136) but through a genuine, dam-free, single-2D-area pure-2D deck -- the exact HEC
6.6 form. The iteration log (the ADR 0133/0136 named-error method):

| # | engine error | fix |
| --- | --- | --- |
| 1 | `error reading header ... storage area ... reach 2D Interior Area` | the perimeter-count graft widened an 8-char fixed field; rewrite the whole line 8-wide (`patch_chippewa_xnn`) |
| 2 | `HDF_ERROR ... output must not already exist` | copy a CLEAN (unsolved) plan HDF, not a solved one |
| 3 | `integer divide by zero` (`hdf_set_compression`) | Chippewa `b02` ships `HYDROGRAPH LOCATIONS = 0`; point at 1 node (`patch_chippewa_bnn`) |
| 4 | **SOLVE COMPLETES (vol err 0.0)** | -- |

## The precise STOP: the 2D-BC-line `.bNN` header is NOT the wetting link -- the plan-HDF 2D-BC `/Event Conditions` schema is

Forcing the fake reach routes the flow **1D in -> out** (flux 6743.8 balanced through
the fake reach's own `Downstream Normal Depth`); it does NOT spill onto the carved 2D
BC line. The computed `Geometry/GeomPreprocess/Reach Connections` confirms the fake
reach is standalone (`NDCON = 0`, `IDSTYP = 0`). A 2D-BC-line inflow is a **2D
external boundary condition**, read by the engine's `read_un_q2d_bc_`
(Read_UN_Q2D_BC.for) from the plan-HDF `/Event Conditions/Unsteady/Boundary
Conditions`. Wetting needs the 2D BC line ENUMERATED there as a 2D flow boundary.

**No file in `Example_Projects_6_6` exposes that schema:** Chippewa's `u04.hdf` holds
only empty Meteorology; the lone shipped plan HDF (Water Quality `p01.hdf`) is not a
2D-BC deck; the Muncie solved fixture has 1D reach hydrographs only. Authoring the
2D-BC `/Event Conditions` entry blind (best-guess keys/attrs) **SEGFAULTs in
`read_un_q2d_bc_`** -- the ADR 0133/0136 no-blind-authoring rule, empirically
re-confirmed. That plan-HDF 2D-BC Event-Conditions schema is the precise OI-FT1 STOP.
Chippewa's geometry BC lines carry no flow-vs-normal marker beyond `Type=External`
(identical to what `write_boundary_condition_lines` authors), so the missing link is
NOT geometry-authorable -- it is the plan-side 2D-BC enumeration.

## Consequences

- No server / tool / contract / registry change; registry byte-identical (git: this
  ADR + `chippewa_reference/` + the `patch_chippewa_{xnn,bnn}` authors + their test +
  the build script). Coded-tools delta: **0** (worker-local authoring components,
  like the ADR 0133/0135 writers). No template pin / category / corpus change -- none
  is warranted until the 2D BC line can be wetted end-to-end (`hecras_flood_2d` stays
  correctly GATED, no template on a dry-2D solve).
- New durable code under `.../subst/crux/`: `chippewa_reference/` (vendored
  `x01`/`b02` + BC-lines schema JSON + decoded README), `freshtopo/hecras_pure2d_deck.py`
  `+~90 LOC` (`patch_chippewa_xnn` / `patch_chippewa_bnn`),
  `freshtopo/build_chippewa_fakereach_deck.py` (the clean-deck build), and
  `freshtopo/test_chippewa_deck.py` (3 offline gates, green). No `flood.py` / SFINCS /
  `publish_layer` / registry reference (grep-verified). No server import-graph change.
- Offline suite: all new files are under `services/workers/` and are NOT collected by
  `server/tests/`; server baseline delta is **zero by construction** (verified: the
  documented failure set is unchanged -- fetch_resolution x4 + river_dye x5).
- Image hygiene: no image built; throwaway `--rm` containers on the pre-existing
  `trid3nt-local/hecras:latest` (2.2 GB). The 432 MB zip + all extractions deleted
  after the sweep. Proofs under `scratchpad/wetting_proofs/`; durable footprint ~15 KB.

## Open issues / ledger

- **OI-FT1 (REFINED, the precise STOP).** The wetting link is NOT the `.bNN` header
  (decoded: the 6.6 fake-reach form) NOR the geometry BC line (authored). It is the
  plan-HDF `/Event Conditions/Unsteady/Boundary Conditions` 2D-BC-line flow-hydrograph
  schema read by `read_un_q2d_bc_`. UNBLOCK: obtain a shipped or RAS-generated plan
  tmp HDF that enumerates a 2D-BC-line flow hydrograph (none exists in this
  distribution) -- e.g. run RASMapper/the GUI on a minimal 2D-BC project to emit one,
  or decode the `read_un_q2d_bc_` expected group/attr layout from a KNOWN-GOOD
  artifact. Then key the carved BC line "Inflow" into Event Conditions, force the fake
  reach in the `.bNN`, and the carve wets (wet cells where the carved terrain is low +
  the x1.5 delta ON the 2D area).
- **OI-FT2 (the template -- QUEUED, unchanged).** `hecras_flood_2d` + its archetype +
  the formalized authoring worker land TOGETHER once OI-FT1 wets, with BOTH
  acceptances (a Muncie self-check + a genuinely-new small US AOI). The geometry +
  fresh-topology + pure-2D-deck halves are now proven; the 2D-BC enumeration is the
  last forcing link.
- Carries ADR 0134 OI-D (precipitation -- Meteorology+DSS residual) and ADR 0132 OI-3
  (the 2025 `ras` build is `-dev`/schema-unstable) + OI-4 (virtual-cell SanityCheck
  NotSupportedException).
