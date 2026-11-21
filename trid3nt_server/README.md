# `trid3nt_server/` - the daemon

The local server the QGIS plugin talks to: one WebSocket process that holds a
turn loop, hosts the harness, dispatches the registered tools and publishes what
they produce back onto the caller's canvas. Everything here runs on one machine
against one user; the only wire shapes it speaks are `trid3nt_contracts`. It
imports `trid3nt` (the SDK) and `trid3nt_harness` (the control plane); neither
imports it back.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door and its version. |
| `__main__.py` | `python -m trid3nt_server` - the way the daemon is started. |
| `main.py` | The `trid3nt-server` console script: importing `trid3nt.tools` is what populates the registry. |

## Subfolders

| subfolder | what lives there |
| --- | --- |
| `render/` | Emission: the session's pipeline state and the one seam a client-bound layer crosses. |
| `server/` | The daemon core: connection loop, turn engine, dispatch, session state. |
