# `workflows/telemac/authoring/` - everything the box receives

A `.cas` is a RECORD of the run - which boundary carries the flowrate, what the
friction law is, which module is coupled - and a record has one author. That
author is here, on the server, beside the sheet the numbers came from. What
travels to the worker is the mesh, the authored decks and the files they name;
nothing the container receives is a knob it has to interpret.

Every optional block is written ONLY when it was asked for, so a run that uses
no module leaves the deck byte-identical to the one it always wrote. Every line
respects DAMOCLES's hard 72-character limit, and every authored deck is parsed
by the engine's own reader against its own dictionary before anything is staged.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door. Consumers import the module they mean. |
| `agitation.py` | The ARTEMIS deck and its deliverable: a swell at the mouth, an agitation field inside. |
| `author.py` | The accepted mesh plus the approved sheet to TELEMAC's own steering files, in the liquid-boundary order the mesh measured. |
| `cas_validate.py` | Every authored steering file, parsed by the engine's own reader against its own dictionary before anything is staged. |
| `deck.py` | The reach family's serialization: params and forcing to the run's own record of what it solves, staged for the box. |
| `oil_templates/` | The user-fortran source an oil-class run compiles into its deck. |
| `open_water.py` | The open-water front of the AOI templates: stage, solve, read, surface. |
| `rain_on_grid.py` | The rain-on-grid front: a catchment in, an outlet hydrograph out. |
| `stratified.py` | The TELEMAC-3D deck and its deliverable: a water column in, its vertical structure out. |

The deck writers and the fronts still overlap - three of these files stage, solve
and publish as well as author. Collapsing them onto one assembler is the next
stage's work, not a claim this tree already makes.
