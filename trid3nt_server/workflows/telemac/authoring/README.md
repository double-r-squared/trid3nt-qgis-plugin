# `workflows/telemac/authoring/` - everything the box receives

A `.cas` is a RECORD of the run - which boundary carries the flowrate, what the
friction law is, which module is coupled - and a record has one author. That
author is here, on the server, beside the sheet the numbers came from. What
travels to the worker is the mesh, the authored steering files and the files they
name; nothing the container receives is a knob it has to interpret.

ONE FLOW. `assembler.py` is where every simulation the server authors becomes a
staged run directory: sheet in, steering file written, every field it names
written beside it, the whole directory uploaded, the manifest written last. What
differs between families is DATA - a recipe row naming the engine class, the file
names and the writer - never a branch in the flow.

Every optional block is written ONLY when it was asked for, so a run that uses
no module leaves the steering file byte-identical to the one it always wrote.
Every line respects DAMOCLES's hard 72-character limit, and every authored file
is parsed by the engine's own reader against its own dictionary before anything
is staged.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door. Consumers import the module they mean. |
| `agitation.py` | The ARTEMIS case and its deliverable: a swell at the mouth, an agitation field inside. |
| `assembler.py` | The approved sheet plus the accepted mesh to the run directory the box receives - the one flow, for a reach and for a catchment alike. |
| `author.py` | The sheet, serialized into TELEMAC's own steering files, in the liquid-boundary order the mesh measured. |
| `cas_validate.py` | Every authored steering file, parsed by the engine's own reader against its own dictionary before anything is staged. |
| `oil_templates/` | The user-fortran source an oil-class run compiles into its steering file. |
| `open_water.py` | The open-water front of the AOI templates: the case section, the one manifest writer, the dispatch, the bed the domain is solved on. |
| `stratified.py` | The TELEMAC-3D case and its deliverable: a water column in, its vertical structure out. |
