# `trid3nt_harness/` - the control plane

What decides and asks: the LLM provider adapters and the guards over a model
turn, the tool surfacing a model reads, and the gate a turn waits on a person
at. It imports `trid3nt` only. Importing it registers the gate the SDK's review
and draw ask, the fill's walk down a ranked list for an input a run leaves unstated, and
the tools only the model calls; it reaches the daemon's wire
through the send the daemon registers on the confirm engine.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door, and the registrations it makes into the SDK. |

## Subfolders

| subfolder | what lives there |
| --- | --- |
| `inputs/` | `gate/`: where a turn waits on a person - the input review, the draw and spatial input, the confirm, the pending registry and the cards. |
| `model/` | The model connection: provider adapters and the guards over a model turn. |
| `tools/` | Tool surfacing: retrieval, the search tools, the kwargs cleanup at the dispatch seam. |
