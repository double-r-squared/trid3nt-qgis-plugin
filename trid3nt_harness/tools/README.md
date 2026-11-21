# `tools/` - tool surfacing for the model

What puts the SDK's registered tools in front of a model: retrieval over the
corpus, the search tools, and the kwargs cleanup at the dispatch seam. The
registry itself is the SDK's (`trid3nt/tools/`).

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door; loads the registry first. |
| `tool_arg_normalizer.py` | Call-site kwargs cleanup, so an invented argument does not fail a call the tool could still take. |
| `tool_query_corpus.yaml` | The routing phrasings tool retrieval scores an ask against. |

## Subfolders

| folder | what it is |
| --- | --- |
| `search/` | The library's asks: `search_tools` (which tool), `find_sources` (which source here), `fetch`, and the `tool_retrieval` ranking the gate reads. |
