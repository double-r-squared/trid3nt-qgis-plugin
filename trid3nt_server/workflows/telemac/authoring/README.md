# `workflows/telemac/authoring/` - everything the box receives

A `.cas` is a RECORD of the run - which boundary carries the flowrate, what the
friction law is, which module is coupled - and a record has one author. That
author is here, on the server, beside the sheet the numbers came from. What
travels to the worker is the mesh, the authored steering files and the files they
name; nothing the container receives is a knob it has to interpret.

TWO ACTS. The run is SETTLED - everything the accepted mesh has to be measured
for before a keyword can be set - and `staging.py` STAGES it:
everything the fill wrote is uploaded beside the mesh the solve runs on, and the
manifest that names the case is written last. `serializer.py` is what turns the filled sheet
into the engine's own steering files, through telapy, and every authored file is
read straight back by the engine's own parser against its own dictionary before
anything is staged.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door. Consumers import the module they mean. |
| `accepted_mesh.py` | What ANY accepted mesh hands the author, under the record's own key names: the files the box is given, the nodes and the bed, the channel a boundary face cuts, the walk over the outline and whether that outline is closed. |
| `mesh_files.py` | THE MESH FILES this engine asks an accepted mesh for: the SELAFIN + `.cli` pair written from ONE walk of the mesh's own nodes, cells and named stretches, recorded on the mesh under the steering keywords, and the boundary numbering that walk measures. |
| `selafin_io.py` | SELAFIN in the daemon, both directions: the pair written as one artifact from the engine's own boundary numbering, refusing a walk whose rings share a node, and a result read back - neither costs a container start. |
| `topology.py` | The numbering a geometry file cannot state: which run of boundary nodes carries which role, the order the solver numbers the liquid boundaries in, and what each prescribes. |
| `opening.py` | THE OPENING: the level the run starts at - the level the question states, the sea it stands in, the normal depth a reach's own discharge holds over the channel its inflow face cuts, or the last surface of the run this one carries on from, staged as its previous computation. |
| `initial_state.py` | THE INITIAL STATE both the opening and the release point read: every node wet for a fresh run, or the last instant and wet/dry field of the restart record a continued run carries on from. |
| `rating_curve.py` | THE RATING CURVE an outlet holds: the normal depth over the section its own face cuts, swept over the flow range the storm can send, at the roughness the deck writes at those same nodes. |
| `reference_surface.py` | THE REFERENCE SURFACE a dredger cuts to, as NESTOR reads levels: the design grade as cross-sections along the domain's own centerline, and the stock under it measured against the bed before the run dispatches. |
| `walked_boundary.py` | THE WALKED BOUNDARY of a harbour: which stretch of the outline the sea reaches, the structure's own segments and the basin's deepest column, so the boundary file prescribes the incident wave there and a solid face nowhere else. |
| `release_point.py` | THE SETTLED RELEASE POINT: where on the accepted mesh a source enters water - along the domain's centerline or held inside it, and onto a node the run's own initial state has water at, off the rim. |
| `staging.py` | The run directory the box receives: the case section every run dispatches under, a fresh run tag and its directory, every authored file uploaded beside the mesh, and the manifest written LAST so one exists only for a fully staged run. No mesh, no geometry, no physical value is read here. |
| `staged_check.py` | The staged run directory read clause by clause before it leaves the daemon: every file the steering names present under the name the box will find, the boundary file counted and numbered against the walk over the geometry's own connectivity, a value at every liquid face the boundary file opens, a series covering the run's window, a bed whole over the mesh, the deck's clock against the window the fill states, and a partition this box can seat. Each clause refuses by name rather than letting the solve stop inside Fortran. |
| `cas_validate.py` | The ONE door to the image for the steering format, in both directions: telapy writes, the engine's own reader reads back, and every authored file is parsed against its own dictionary before anything is staged. |
| `serializer.py` | A sheet of raw keywords, written by telapy as the engine's own steering file and read straight back by the engine's own parser. The ONE writer of the steering format. |
| `atmosphere.py` | The weather over the domain as ONE table: the value both hosts state it through, the column mnemonics BIEF's own reader searches the header for, the columns each host's own source term reads, and the carriage of a fetched station record into them. Shared because the reader is BIEF's rather than either host's. |
| `boundaries.py` | The values an open edge carries instant by instant: the column name the engine builds from a boundary's own number, and the table its reader scans. Shared because ``read_fic_frliq`` is what both hosts' boundary routines call. |
| `oil.py` | The oil module's two input files as content: the preset in the module reader's own format, and the release routine this run's step and point are compiled into. |
| `nestor.py` | The dredge's three input files as content: the actions in the reader's own `Keyword = value` grammar, the polygons that name the fields the three leading numerals identify them by, and the surface reference its levels are read from. |
| `oil_templates/` | The engine's own release routine, shipped here because the release coordinates are compiled INTO it. |
