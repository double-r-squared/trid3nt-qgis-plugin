# `scripts/` - what you type, and what measures the tree

The eight shell entry points at this level are the ones the README and the
Makefile name: they start, stop, install and build. Everything below is a lane
rather than an entry point - instruments that measure, renderers that draw a
packet, drivers that put a real question through the running daemon, stagers
that publish a dataset once.

## Files

| file | what it is |
| --- | --- |
| `build_telemac_image.sh` | Build the local TELEMAC worker image. |
| `fetch_binaries.sh` | Idempotent downloader for `mf6`, `minio` and `mc`. |
| `init_minio.sh` | Create the runs and cache buckets in the local MinIO. |
| `install_plugin.sh` | THE plugin deploy step: sync the plugin into the live QGIS profile. |
| `package_plugin.sh` | Package the plugin into the daemon-served custom repository. |
| `start_agent.sh` | Start the daemon for local dev, loading `.env.local`. |
| `start_minio.sh` | Start MinIO, writing its pid and log under `run/` and `logs/`. |
| `use_openrouter.sh` | Point the daemon at OpenRouter, or back at a local Ollama. |

## Subfolders

| subfolder | what lives there |
| --- | --- |
| `instruments/` | What measures and checks the tree: the model checker, the code graph, the LOC report, the tool sweep, the catalog extractor, the generated pages. |
| `packet/` | The proof-packet renderers and the doc-sized figures a template page embeds. |
| `drivers/` | The live drive lane: one declared question per script, run against the daemon the way the plugin drives it. |
| `staging/` | One-shot stagers that publish a source dataset into object storage. |
