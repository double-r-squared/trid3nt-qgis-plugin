# TRID3NT Local

A **QGIS plugin plus a local daemon**: ask in plain language, get real layers on
your own canvas, on your own machine. This repo IS the product - the plugin (the
only client) and the server it drives; one clone is a working end-to-end setup.

## What it can do today

- **Fetch measured data** from 99 declared sources through one router - terrain,
  hydrology, climate, weather, imagery, ocean, soil, hazard, socioeconomic - as
  native QGIS layers, never a download folder.
- **Run the geoprocessing in your own QGIS session**: any Processing algorithm
  over the layers on the map, and anything the tools do not cover written as a
  confirmed PyQGIS snippet the plugin runs in place.
- **Author, mesh, run and read back** a TELEMAC hydrodynamic run: a real reach or
  catchment, a triangulated domain, a solved result, and the layers, charts and
  animation that came out of it.

## The one loop

Ask -> the router fetches what the question needs -> the mesh front builds a
recipe for the domain and holds it at a gate -> a template fills the module sheet
-> the box solves -> layers, charts and a delivery packet land in QGIS.

## The module surface

A template declares raw engine keywords over a wrapper built from the engine's
own dictionary. `fill` sets the sheet and decides nothing; `run` serializes it,
stages the run directory and hands it to the box. Nothing is hidden: every
keyword the template does not state keeps the engine's own default, and
`describe_keywords` names it with that default so you can set it yourself.
The wrappers are in [docs/modules.md](docs/modules.md).

## The engines

TELEMAC in a container - TELEMAC-2D and 3D, ARTEMIS, WAQTEL and GAIA. Eight
registered templates, one per question. Each page carries its declaration, its
proving run and that run's figures: [docs/templates/index.md](docs/templates/index.md).

| template | the question it answers |
| --- | --- |
| `telemac_river_dye` | A dye / tracer / contaminant plume travelling downstream in a river. |
| `telemac_river_oil_spill` | An oil slick on a river: floating particles plus the dissolved fraction. |
| `telemac_river_scour` | Bed scour and deposition in a reach: a mobile bed under a flow. |
| `telemac_river_sediment_plume` | A suspended sediment plume that settles and deposits on the bed. |
| `telemac_do_sag` | The dissolved-oxygen sag below a discharge (the TMDL / permit question). |
| `telemac_rain_on_grid` | How much runoff a storm produces from a watershed, as a hydrograph and a depth map. |
| `artemis_harbor_agitation` | The wave agitation a declared structure leaves inside a harbour. |
| `telemac3d_stratified_flow` | The 3D vertical structure a depth-averaged model cannot resolve. |

## Install

Three paths, depending on the machine. Full walkthrough, prerequisites and
troubleshooting: [docs/site/install.md](docs/site/install.md).

| Path | Machine | Steps | Needs QGIS? | Needs git/venv/docker? |
|------|---------|-------|-------------|-------------------------|
| **Daemon-only** | the box that runs the server | `git clone` + `make setup && make up` | No - `plugin/` is inert | Yes |
| **Client-only** | a laptop that just wants the dock | `make plugin-zip` on *any* checkout, copy `dist/trid3nt-plugin-<version>.zip` over, then QGIS: **Plugins > Install from ZIP**, then **Settings > Server URL** | Yes | No |
| **Both** | one dev machine | `git clone` + `make setup && make up && make plugin` | Yes | Yes |

```sh
make setup     # one-time: write .env.local, fetch binaries, build the agent venv
make up        # start the stack: minio + agent
make plugin    # install the plugin into your QGIS profile, then reload it in QGIS
make status    # health-check the services
make test      # the six suite slices, zero failures
```

Prerequisites: Linux x86_64, Python 3.12, [uv](https://astral.sh/uv), Docker (for
the container solvers; your user must be in the `docker` group), and an LLM
endpoint. A client-only machine needs QGIS 3.28+ and nothing else.

## The LLM

Pluggable: any OpenAI-compatible endpoint (Ollama, vLLM, llama.cpp, LM Studio;
OpenAI, Groq, DeepSeek, OpenRouter) or Anthropic. Set it in `.env.local` -
`MODEL_PROVIDER`, `TRID3NT_OPENAI_BASE_URL`, `TRID3NT_OPENAI_MODEL`,
`TRID3NT_OPENAI_API_KEY` - and switch the model live from the dock's Settings
without a restart. `scripts/use_openrouter.sh <KEY> [model]` writes the block for
you. The full reference is [docs/site/configuration.md](docs/site/configuration.md).

## Service URLs

| Service | URL | Notes |
|---|---|---|
| Agent WS | ws://localhost:8765 | what the plugin connects to |
| Agent HTTP | http://localhost:8766 | tool catalog + telemetry |
| MinIO API | http://localhost:9000 | S3-compatible object storage |
| Ollama | http://localhost:11434 | optional local LLM |

A client machine points its **Server URL** at the daemon's
[Tailscale](https://tailscale.com) address instead of loopback, e.g.
`ws://100.x.x.x:8765/ws`; everything else is advertised on connect. Set
`TRID3NT_ACCESS_TOKEN` on the daemon for a shared-secret lock - see
[Remote daemon access](docs/site/configuration.md#remote-daemon-access-tailnet).

## Repo layout

Each has its own map README.

| directory | what it is |
| --- | --- |
| `plugin/` | The QGIS plugin - the only client. |
| `trid3nt_server/` | The daemon: turn loop, tool dispatch, gates, render, workflows. |
| `contracts/` | The shared pydantic contracts both sides import. |
| `workers/` | The solver worker images - the engine room. |
| `scripts/` | The entry points you type, and the model checker. |
| `dev/` | The dev tools - lints, instruments, packet renderers, drivers, the live harness. Not product, not on the remote. |
| `tests/` | The suite, one directory per subsystem. |
| `docs/` | Method, rulings and maps. |

## Deploy seams

Three, independent; a git commit deploys none of them.

- **Server code**: edit `trid3nt_server/`, then `make agent` - the venv installs it editable, so a restart picks the change up.
- **QGIS plugin**: `make plugin`, then reload in QGIS. QGIS runs the installed profile copy, never this checkout.
- **Worker image**: `scripts/build_telemac_image.sh`. A worker edit is inert until the image is rebuilt.

## Where the documentation lives

`docs/site/` the manual · `docs/templates/` the template gallery ·
`docs/modules.md` the engine wrappers · `docs/authoring/` how to extend it ·
`docs/model/` the suite-checked model · `docs/design/` how a feature works ·
`AGENTS.md` the charter.
