# `trid3nt_server/` - the daemon

The local server the QGIS plugin talks to: one WebSocket process that holds a
turn loop, dispatches the registered tools, drives the engine workflows and
publishes what they produce back onto the caller's canvas. Everything here runs
on one machine against one user; the only wire shapes it speaks are
`trid3nt_contracts`.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door and its version. |
| `__main__.py` | `python -m trid3nt_server` - the way the daemon is started. |
| `main.py` | The `trid3nt-server` console script: importing `trid3nt_server.tools` is what populates the registry. |
| `persistence.py` | The typed wrapper over the document store: cases, layers, chat, run snapshots. |
| `plugin_repo.py` | The QGIS custom plugin repository the daemon serves: the versioned zip, `plugins.xml` and its manifest. |
| `telemetry.py` | The JSONL sink: one line per tool call, turn, shadow selection and solve completion. |

## Subfolders

| subfolder | what lives there |
| --- | --- |
| `adapters/` | The LLM provider adapters, behind one shared IR. |
| `cases/` | Case-side layer serving: ingesting a pushed layer, probing a point. |
| `credentials/` | The connect handshake and the per-provider credential registry. |
| `emission/` | Everything a computed layer passes through on its way to the map. |
| `fallbacks/` | Declared degradation: ladders as data, and the one walker. |
| `gates/` | The agent-loop gates - confirm, review, draw, budget, runaway. |
| `sandbox/` | The code-exec box: the container a user-confirmed snippet runs in. |
| `server/` | The daemon core: connection loop, turn engine, dispatch, session state. |
| `tools/` | The registered tool surface: fetchers, derive, search, display. |
| `workflows/` | The declarative engine layer: the runtime, the mesh front, TELEMAC. |
