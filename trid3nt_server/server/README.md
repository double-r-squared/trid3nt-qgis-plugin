# `server/` - the daemon core

The connection loop, the turn engine, tool dispatch and session state. This
package holds what is true of the process rather than of any one question: which
sockets are open, which turn is running, what a tool call is allowed to do and
where its results go.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The core's door; the subpackages below hold the working parts. |
| `config.py` | Environment-knob readers, each a pure `env -> value` read taken live. |
| `errors.py` | `ToolNotFoundError`, the dispatch path's refusal of a name nothing registered. |
| `spatial.py` | The zoom-to helpers: the bbox the camera snaps to for a result, deduped against this turn. |

## Subfolders

| subfolder | what lives there |
| --- | --- |
| `dispatch/` | One tool call end to end: AOI, emission, result summaries and persistence. `case_script.py` keeps the reviewed case or calibration script and served pins. `script_child.py` runs the script, routes deferred study reviews through the daemon, and forwards progress and recorded outputs. `trid3nt_server/server/dispatch/remote_run.py` links portable runs and relays the same messages on the authenticated runner WebSocket. |
| `protocol/` | The wire: authentication, the connection registry, the message handlers, the HTTP door (the library and the data routes) and the accept loop. |
| `session/` | What one connection holds - the case it is on, its persistence handle, its mutable state. |
| `turn/` | The turn engine: the model stream, its compaction card, the wire envelopes it produces, and the case bookkeeping around it. |
