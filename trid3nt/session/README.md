# `session/` - requests a live session answers

A tool that runs in the user's own QGIS session asks it through the emitter bound
to the call (`trid3nt/render/emission.py`) and parks on the reply. With nothing
bound there is no session, and every request refuses `SESSION_UNAVAILABLE`:
nothing invents a result.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door. |
| `pending.py` | Owner-checked pending replies, and the two registries a session request parks on (processing, layer). |
| `processing.py` | The session request seam: one `processing-request` emitted and its response awaited, bounded and owner-checked, with the typed refusals. |
