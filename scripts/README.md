# `scripts/` - what you type

Entry points that SHIP: they start, stop, install and build the local stack,
plus the one checker the suite runs. What measures, renders, drives or stages is
a dev tool and lives under `dev/`, which git does not carry.

## Files

| file | what it is |
| --- | --- |
| `build_telemac_image.sh` | Build the local TELEMAC worker image. |
| `fetch_binaries.sh` | Idempotent downloader for `mf6`, `minio` and `mc`. |
| `init_minio.sh` | Create the runs and cache buckets in the local MinIO. |
| `install_plugin.sh` | THE plugin deploy step: sync the plugin into the live QGIS profile. |
| `model_check.py` | The SysML model checker the suite runs, and the generator of every `<seam>-view.md`. |
| `package_plugin.sh` | Package the plugin into the daemon-served custom repository. |
| `start_agent.sh` | Start the daemon for local dev, loading `.env.local`. |
| `start_minio.sh` | Start MinIO, writing its pid and log under `run/` and `logs/`. |
| `use_openrouter.sh` | Point the daemon at OpenRouter, or back at a local Ollama. |
