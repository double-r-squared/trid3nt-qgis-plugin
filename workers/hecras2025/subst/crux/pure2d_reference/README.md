# HEC-RAS pure-2D forcing reference (OI-A discharge)

The shipped **pure-2D reference deck** ADR 0133 recorded as missing. Extracted from
HEC's SHA-pinned `Example_Projects_6_6.zip`
(sha256 `ea239b506155a2dfeda2af80b3c2af948eef42c40218bcd65de472cfed386887`,
432389121 B -- the same distribution ADR 0132 used for the Muncie terrain),
`2D Unsteady Flow Hydraulics/BaldEagleCrkMulti2D/`. Public-domain USACE software;
CRLF stripped for repo hygiene. These files answer ADR 0133 OI-A: how a PURE-2D
project expresses a 2D-BC-line inflow hydrograph and precipitation, in BOTH the
Windows-era ASCII AND the Linux-consumed intermediates.

## The key discovery: the zip DOES ship Linux pure-2D intermediates

ADR 0133 assumed "the shipped Windows projects lack the Linux intermediates
(`.bNN` etc)" and "NEITHER [forcing] appears in any in-repo reference". That premise
is SUPERSEDED: BaldEagle plan 06 ("Gridded Precip - Infiltration", `Flow File=u03`,
`Geom File=g09`) ships its **preprocessed Linux boundary file `.b06` AND geometry
preprocessor `.x09`** -- a working, HEC-authored **pure-2D** reference. The `.b06`
IS the missing `.bNN` reference; its format is proven-valid by construction (HEC's
own preprocessor emitted it). No blind authoring / empirical RasUnsteady iteration
is needed to learn the stanza -- it is here.

## Files

| file | what it is |
| --- | --- |
| `BaldEagleDamBrk.b06` | the Linux `.bNN` boundary file (plan 06, pure-2D). THE reference. |
| `BaldEagleDamBrk.x09` | the Linux `.xNN` geometry-preprocessor file for the pure-2D `g09`. |
| `BaldEagleDamBrk.u02` | Windows unsteady flow "Single 2D Area with Bridges" (2D BC-line inflow + DS normal depth). |
| `BaldEagleDamBrk.u03` | Windows unsteady flow "Gridded Precipitation" (2D BC-line inflow + `Met BC=Precipitation` gridded-DSS block). |
| `BaldEagleDamBrk.u09` | Windows unsteady flow "Upstream 2D" (a clean 2D-BC-line `Flow Hydrograph`, area `Upstream2D` / BC line `USFlow`). |
| `BaldEagleDamBrk.p06` | the plan file tying `u03` + `g09` together. |
| `g09_hdf_schema.json` | the `.g09.hdf` group schema (the 11 MB HDF itself is NOT vendored) -- the `/Geometry/Boundary Condition Lines/` datasets + the 3 BC-line rows. |

## The forcing decoded

### 1. 2D-BC-line inflow hydrograph -- AUTHORABLE (the landing forcing)

**Windows `.u` form** (`Boundary Location=` field 6 = 2D area, field 8 = BC line):

    Boundary Location=  , , , ,          ,Upstream2D      ,          ,USFlow          
    Interval=1HOUR
    Flow Hydrograph= 204
        5000 5229.73 ...          (FLOW-only ordinates, time implicit via Interval)

**Linux `.bNN` form** (`b06`, inside `Hydrograph Data`): a **BARE** header -- NO
`River:/Reach:/RS:` suffix, NO `2D:` prefix (that suffix is exactly what marks a
1D-reach inflow; its ABSENCE marks a 2D-BC-line inflow):

    Hydrograph Data
           2
           F       F       T       F       F               F
    Upstream Flow Hydrograph
           2
           0     100    8760     100      (explicit TIME,FLOW pairs, 8-char fixed fields)
     3.4E+38
           F       F       F       T       F
    Downstream Normal Depth
        .001

The `.u`->`.bNN` transform is the same one Muncie's own `u01`->`b04` pair shows
(`docs/decisions/0132/0133`): `Interval=` + flow-only ordinates expand to explicit
`(time, flow)` pairs; the `Boundary Location=` fields become the header. For a 2D
BC line the header is the bare family name; the mapping to a SPECIFIC BC line is
**positional** against the geometry's BC-line list (by type: flow hydrographs in
`Hydrograph Data`, normal depths separately). `deck_edit.scale_flow_hydrograph`
already matches `Flow Hydrograph` bare headers, so the existing flow-scaler works on
this stanza unchanged.

### 2. Precipitation (rain-on-grid) -- NOT in the `.bNN` (stays a named residual)

`u03` carries precipitation as a `Met BC=Precipitation|Mode=Gridded` block pointing
at `Gridded DSS Filename=.\Precipitation\precip.2018.09.dss` +
`Gridded DSS Pathname=/SHG/MARFC/PRECIP/.../NEXRAD/`. Crucially, the Linux `.b06`
for THIS SAME precip plan contains **NO precipitation stanza at all** -- only the 2D
BC-line inflow + normal depth. Precipitation forcing does NOT live in the `.bNN`; it
lives in the plan HDF Meteorology group + a **binary HEC-DSS** grid file. So precip
is NOT authorable as a `.bNN` edit; it needs the plan-HDF Meteorology structure + a
DSS writer (or a constant/uniform-hyetograph path, whose `.bNN`/HDF serialization
still has no shipped reference here). **Verdict (confirms ADR 0133): the flow-forced
2D BC line is the landing forcing; precipitation stays a named residual** -- higher
risk, deferred to a Meteorology+DSS authoring wave (the Atlas-14 seam feeds it once
that path exists).

## The pure-2D deck architecture (decoded, for the authoring worker)

A genuinely-new pure-2D AOI deck the Linux engines solve needs:

1. **Geometry HDF** `/Geometry/2D Flow Areas/<name>/` (the `hecras_geometry_writer`
   already authors this, solver-validated ADR 0133) **PLUS**
   `/Geometry/Boundary Condition Lines/` -- the writer does NOT yet author this.
   Schema (from `g09_hdf_schema.json`): `Attributes` compound
   `[Name S32, SA-2D S16, Type S8, Length f4]`, `External Faces` compound
   `[BC Line ID i4, Face Index i4, FP Start i4, FP End i4, Station Start f4,
   Station End f4]` (each BC line -> the perimeter faces it spans), and
   `Polyline Info/Parts/Points` (the BC-line polyline geometry).
2. **`.xNN`** (from `x09`): declares the 2D area as a **Storage Area** (`SA  8 ...
   BaldEagleCr`), a minimal **fake 1D reach** (`Fake River`/`Fake Reach` -- the
   engine requires >=1 reach), Arrays Sizes counts that must match the mesh, and the
   PropertyTableOptions (`Cell Vol Tol .01`, `Face Conv Ratio .02`, ...).
3. **`.bNN`** (from `b06`): the bare positional `Upstream Flow Hydrograph` +
   `Downstream Normal Depth` (section 1 above).
4. **Plan HDF Event Conditions** (authorable per ADR 0133); + Meteorology only for
   the precip residual.

Links 2 + the BC-line HDF group + a full-topology `.NET` mesh dump are the remaining
build (see ADR 0134). Link 3 (`.bNN` forcing) -- the one ADR 0133 walled on -- is
DISCHARGED: the reference is `b06`.
