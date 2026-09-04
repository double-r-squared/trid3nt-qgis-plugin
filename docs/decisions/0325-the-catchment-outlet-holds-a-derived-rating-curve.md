# 0325 - The catchment outlet holds a derived rating curve

A rain-on-grid catchment drains through one face, and until now that face
prescribed a water LEVEL with no value written at its number. `bord.f` then fell
through to the boundary file's own zero, so the outlet was a hard zero-DEPTH
Dirichlet: measured on the Coweeta design storm, all three outlet nodes held
0.0000 m in every frame. The alternative already tried - an all-KSORT free exit -
is well-posed only while the normal velocity leaves, and `propin_telemac2d.f`
refused it by name the moment flow entered (14 warnings, +29,425 m3/s injected
in one printout, a runoff volume of zero, with CORRECT END OF RUN).

A subcritical outlet needs one fact from outside. This note records which fact,
and how it is derived rather than chosen.

## The decision

The outlet holds a **derived stage-discharge curve**, written as TELEMAC's own
`STAGE-DISCHARGE CURVES = 1` (Z(Q)) at the outlet's measured liquid-boundary
number, with the curve in the `STAGE-DISCHARGE CURVES FILE` beside the deck.
`bord.f` reads it at every prescribed-depth boundary whose entry is 1,
interpolates the elevation against that boundary's own measured flux and relaxes
the depth toward it, so the outlet level rises and falls with the storm.

The curve is the SAME uniform-flow derivation the reach's outflow stage already
was - one closure, two callers - evaluated over a range instead of at one
discharge. Every input is measured on the accepted mesh:

| input | measured as |
| --- | --- |
| the channel | the section the outlet face cuts through the painted bed, in boundary-walk order |
| the friction slope | the plane fitted through the painted nodes of the elements that face belongs to |
| the roughness | the median of the run's own distributed Manning field over the outlet nodes |
| the law | the deck's own `LAW OF BOTTOM FRICTION` |
| the flow range | see below |

Nothing external enters - no gauge, no second datum. A gauged curve is the
calibration-era swap through this same keyword, which is why the mechanism is a
curve and not a constant.

## The range basis

The top of the range is the **gross rain rate on the meshed catchment**: the
storm's peak intensity (the design rate, or the largest hourly block of a real
hyetograph) times the triangulation's own summed area. That is a BOUND rather
than a guess - every drop the run holds falls on the mesh, infiltration only
removes water and storage only delays it - so no outlet flux can exceed it. The
curve is swept from the dry section up to the stage that ceiling stands at, in
20 points spaced uniformly in DISCHARGE - the quantity the engine looks the curve
up by, so every interval's slope is the channel's own dZ/dQ there.

Spacing them evenly in STAGE instead was tried first and is wrong, measured: it
crushes the low end into a first interval carrying 0.02 m3/s across 8 cm of
stage - 4.25 m of level per m3/s on the Coweeta outlet - and a boundary that
swings metres on a trickle sits above a catchment that has not started running
off yet and lifts water back into it. On the design storm that showed as 26 of
60 listing samples with the outlet flux ENTERING, about 1e5 m3 over twelve hours,
against zero entering samples on the run it replaced.

Above the last point the engine holds the last level, so a ceiling below the flow
that arrives caps the outlet rather than extrapolating a channel nobody measured.

## What it cost the boundary contract

The role-to-quad table gains a `rating_curve` row. Its codes are the outflow's,
because `bord.f` reads a curve only where `LIHBOR = KENT`, so quad-derived
`prescribes` reads `"elevation"` for both. The ROLE is what tells them apart:
an `outflow` owes a constant in `PRESCRIBED ELEVATIONS`, a `rating_curve` owes
`STAGE-DISCHARGE CURVES = 1` at its number and the curve file. This is the same
shape as the `free_exit` row, which the steering author already reads by name to
tell a stated silence from a disagreement. One table, two files, still.

## What refuses

A flat bed at the outlet has no uniform-flow depth; an unpainted face has no
section; a friction field with no positive roughness there has no conveyance; a
storm of zero has no range; a friction law whose coefficient is not a conveyance
has no equation. Each refuses typed. Defaulting past any of them would put the
clamp back with nothing saying so.
