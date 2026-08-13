# pump_station -- schema dump + VERIFY results (ADR 0171 row 3)

## ASCII pump-station schema (Pumps.g01 -- fixed trigger elevations)

From `Pumps.g01` (line ~1101):

```
Pump Station=Pumps           ,0.2705995,0.0802043,,,0.2930514,0.0721857
Pump Station From=                ,                ,        ,Bayou           ,
Pump Station To=Beaver Creek    ,Kentwood        ,5.39    ,                ,0
Pump Station Reference=                ,                ,        ,Bayou           ,
Pump Station TW Min=220
Pump Station Group=Group #1        ,False,,
Pump Station Group Pump=Pump #1         ,206,205
Pump Station Group Pump=Pump#2          ,206.2,205.2
Pump Station Group Pump=Pump#3          ,206.5,205.5
Pump Station Group HQ= 6
       0     300      10     270      15     240      18     180      20      90
      21       0
```

Reading: a named `Pump Station` (here "Pumps") with a FROM node (a "Bayou"
storage/interior area) and a TO node (`Beaver Creek`, river station 5.39 --
the receiving water body); a `TW Min` (tailwater minimum elevation, 220 ft);
one `Pump Station Group` containing 3 pumps, each `Pump Station Group
Pump=<name>,<WSEL On elev>,<WSEL Off elev>` (e.g. Pump #1 turns ON at WSEL
206, OFF at 205); and a shared `Pump Station Group HQ=` head-discharge
curve (6 points: head 0 -> 300 cfs, head 10 -> 270 cfs, ... head 21 -> 0
cfs -- a monotonically-decreasing pump curve, real hydraulic data, not a
placeholder).

## ASCII pump + RULE-OPERATION schema (PumpRule.g01 + PumpRule.u02 -- the
## `gate_pump_rules` target)

`PumpRule.g01` (line ~289) -- a second, independent pump station on a
different river (`RedFox`), also 3 pumps:

```
Pump Station=PUMP STA #1     ,0.2869813,0.4033304,,,0.2869813,0.4033304
Pump Station From=RedFox          ,RedFox          ,4       ,                ,
Pump Station To=RedFox          ,RedFox          ,1       ,                ,
Pump Station Reference=                ,                ,        ,                ,
Pump Station Group=Group #1        ,False,5,5
Pump Station Group Pump=Pump #1         ,200,150
Pump Station Group Pump=Pump #2         ,200,106.5
Pump Station Group Pump=Pump #3         ,200,106.525
Pump Station Group HQ= 2
```

`PumpRule.u02` carries a real `Rule Operation=`/`Rule Expression=` script
(the classic HEC-RAS "Trigger"/"Rule" language) that GATES Pump #1 on the
OPEN/CLOSE state of a companion inline gate (`RedFox` RS 2.5, `Gate #1`),
and dynamically RESETS Pump #2/#3's WSEL-On trigger elevations depending on
that gate state -- i.e. real conditional pump-ramp/rule control, not a
static on/off pump:

```
Rule Operation=Type=2,Var Name=GateOpen,Var Type=1,River=RedFox,Reach=RedFox,
  RS=2.5,Gate=Gate #1,PumpGroup=Group #1,PumpName=Pump #1,
  Sim Group=Inline Structures,Sim Function=Gate.Opening (target position),Time=1
Rule Operation=Type=4,...,BranchCompare1=1,BranchCompare2=0   # IF branch
Rule Expression=,Variable=GateOpen
Rule Expression=,Constant=0.1
  ... (comment: "The gate is closing. Turn Pump #1 on. ... Set new WSEL On
       elevation for Pump #2 and Pump #3 ... 106.8 and 106.9")
Rule Operation=Type=3,Var Type=1,PumpGroup=Group #1,PumpName=Pump #1,
  Sim Group=Pump Stations,Sim Function=Turn Pump On
Rule Operation=Type=3,Var Type=1,PumpGroup=Group #1,PumpName=Pump #2,
  Sim Group=Pump Stations,Sim Function=WSEL On
Rule Expression=,Constant=106.8
Rule Operation=Type=3,Var Type=1,PumpGroup=Group #1,PumpName=Pump #3,
  Sim Group=Pump Stations,Sim Function=WSEL On
Rule Expression=,Constant=106.9
Rule Operation=Type=4,...,BranchCompare1=3,BranchCompare2=0   # ELSE/second branch
  ... (comment: "The gate is opening. Turn Pump #1 off. ... Resest WSEL On
       to a high value ... to prevent these two pumps from turning on too
       soon during the next rising hydrograph.")
Rule Operation=Type=3,Var Type=1,PumpGroup=Group #1,PumpName=Pump #1,
  Sim Group=Pump Stations,Sim Function=Turn Pump Off
```

This is a genuine, GUI-authored reference for the `gate_pump_rules` /
`pump_station_trigger_and_ramp_control` rows -- both the STATIC schema
(`Pump Station Group Pump=<name>,<on_elev>,<off_elev>` + `Pump Station
Group HQ=` curve) and the DYNAMIC rule-scripting schema (`Rule Operation=`/
`Rule Expression=` branch/compare opcodes) are now real ASCII examples in
the repo for the first time -- previously zero pump references existed
anywhere in the fixture tree.

## VERIFY: RasGeomPreprocess against `trid3nt-local/hecras:latest`

Same wall as every other fixture in this job -- confirmed on BOTH pump
projects:

```
$ RasGeomPreprocess Pumps.p01.hdf g01       # Pumps.p01.hdf does not exist
$ RasGeomPreprocess PumpRule.p03.hdf g01    # PumpRule.p03.hdf does not exist

forrtl: severe (29): file not found, unit 5, file /data/io.x
  htabopen_ (Htabopen.for:107) <- MAIN__ (Htab.for:33)
```

Neither project ships ANY HDF (geometry or plan) -- both predate HEC-RAS's
HDF5 output entirely (`Pumps.p01` carries no `Program Version` line at all
in some fields, consistent with a pre-5.0.x vintage). No compute was
possible; the ASCII schema above is the deliverable for this fixture, per
the mission's "if the zip ships only ASCII, seed the ASCII anyway" clause.

## What this unblocks

- A real, two-independent-example ASCII schema for BOTH static pump
  trigger/HQ authoring and dynamic rule-based pump control -- the ADR 0171
  row-3 recipe's "extract its pump-group HDF datasets" is not achievable
  (no HDF exists to extract from, confirmed above), but the ASCII
  `Pump Station *` / `Rule Operation=` grammar is now a genuine reference
  for a text-based `write_pump_group(...)`/rule-authoring editor -- the
  same style of ASCII-editing machinery `deck_edit.py` already proved out
  for Muncie's `.bNN` breach/flow blocks.
