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
| `processing.py` | The session request seam: a `processing-request` emitted for the user's QGIS session, the bounded wait, and the typed refusals for no session, no answer and an error answer. |
| `spatial.py` | The zoom-to helpers: the bbox the camera snaps to for a result, deduped against this turn. |

## Subfolders

| subfolder | what lives there |
| --- | --- |
| `dispatch/` | One tool call end to end: the AOI it runs over, the emitter it publishes through, how its results are summarized, persisted and reused. |
| `protocol/` | The wire: authentication, the connection registry, the message handlers, the HTTP door (the library and the data routes) and the accept loop. |
| `session/` | What one connection holds - the case it is on, its persistence handle, its mutable state. |
| `turn/` | The turn engine: the model stream, its compaction card, the wire envelopes it produces, and the case bookkeeping around it. |
