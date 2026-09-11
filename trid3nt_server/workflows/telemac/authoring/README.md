# `workflows/telemac/authoring/` - everything the box receives

A `.cas` is a RECORD of the run - which boundary carries the flowrate, what the
friction law is, which module is coupled - and a record has one author. That
author is here, on the server, beside the sheet the numbers came from. What
travels to the worker is the mesh, the authored steering files and the files they
name; nothing the container receives is a knob it has to interpret.

TWO ACTS. `assembler.py` SETTLES a run - everything the accepted mesh has to be
measured for before a keyword can be set - and then STAGES it: everything the
fill wrote is uploaded beside the mesh the solve runs on, and the manifest that
names the case is written last. `serializer.py` is what turns the filled sheet
into the engine's own steering files, through telapy, and every authored file is
read straight back by the engine's own parser against its own dictionary before
anything is staged.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door. Consumers import the module they mean. |
| `assembler.py` | What the accepted mesh MEASURES before a keyword is set - the bed at its roles, the section its outflow face cuts, the depth that section conveys the flow at, where a source point lands, which stretch of a harbour boundary is the liquid edge, how deep a basin's deepest column is - the case section every run dispatches under, and the staging that turns a filled sheet into the run directory the box receives. |
| `dredging.py` | NESTOR's three files as CONTENT - the fields a maintenance dredge acts on, the actions it takes and the grade it digs to - cut on the accepted mesh by the producer a template names. |
| `cas_validate.py` | The ONE door to the image for the steering format, in both directions: telapy writes, the engine's own reader reads back, and every authored file is parsed against its own dictionary before anything is staged. |
| `serializer.py` | A sheet of raw keywords, written by telapy as the engine's own steering file and read straight back by the engine's own parser. The ONE writer of the steering format. |
| `oil.py` | The oil module's two input files as content: the preset in the module reader's own format, and the release routine this run's step and point are compiled into. |
| `oil_templates/` | The engine's own release routine, shipped here because the release coordinates are compiled INTO it. |
