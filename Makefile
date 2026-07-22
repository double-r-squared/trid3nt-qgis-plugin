# Makefile -- TRID3NT Local runtime targets
REPO_ROOT := $(shell pwd)
SCRIPTS   := $(REPO_ROOT)/scripts
RUN_DIR   := $(REPO_ROOT)/run
LOG_DIR   := $(REPO_ROOT)/logs

.PHONY: binaries minio agent venv status stop setup up down plugin env help

# ---- orchestration (the clone -> run flow) ----------------------------------
help:
	@echo "TRID3NT Local -- bundled local server + QGIS plugin"
	@echo ""
	@echo "  make setup    one-time: create .env.local, fetch binaries, build the agent venv"
	@echo "  (edit .env.local: set your LLM endpoint/key -- see .env.openrouter.example)"
	@echo "  make up        start the local stack (minio + agent)"
	@echo "  make plugin    install the QGIS plugin into your QGIS profile (then reload it)"
	@echo "  make status    health-check the running services"
	@echo "  make down      stop everything"

# One-time bootstrap: env template + binaries (minio/mf6/...) + the agent venv.
setup: env binaries venv
	@echo "setup done. Edit .env.local (LLM endpoint + key), then: make up"

# Create .env.local from the template on first run (never clobber an existing one).
env:
	@if [ -f $(REPO_ROOT)/.env.local ]; then echo ".env.local exists - not clobbering"; else \
	printf '%s\n' \
	  '# TRID3NT local env - fill in your LLM endpoint + key' \
	  'MODEL_PROVIDER=openai' \
	  'GRACE2_OPENAI_BASE_URL=https://openrouter.ai/api/v1' \
	  'GRACE2_OPENAI_MODEL=anthropic/claude-3.5-haiku' \
	  'GRACE2_OPENAI_API_KEY=sk-or-v1-REPLACE-ME' \
	  'AWS_ENDPOINT_URL=http://127.0.0.1:9000' \
	  'AWS_ACCESS_KEY_ID=minioadmin' \
	  'AWS_SECRET_ACCESS_KEY=minioadmin' \
	  'AWS_DEFAULT_REGION=us-east-1' \
	  'GRACE2_CACHE_BUCKET=trid3nt-cache' \
	  'GRACE2_DEV_PERSISTENCE_DIR=$(REPO_ROOT)/data/persistence' \
	  'GRACE2_CATALOG_YAML=$(REPO_ROOT)/public_data_source_catalog.yaml' \
	  > $(REPO_ROOT)/.env.local; \
	echo "wrote .env.local - edit it and set your API key"; fi

# Start the backend stack the plugin talks to (minio must precede the agent).
up: minio agent
	@echo "stack up. Install the plugin (make plugin) + reload QGIS."

down: stop

plugin:
	@bash $(SCRIPTS)/install_plugin.sh

binaries:
	@bash $(SCRIPTS)/fetch_binaries.sh

minio:
	@mkdir -p $(LOG_DIR) $(RUN_DIR)
	@bash $(SCRIPTS)/start_minio.sh
	@bash $(SCRIPTS)/init_minio.sh

agent:
	@mkdir -p $(LOG_DIR) $(RUN_DIR)
	@bash $(SCRIPTS)/start_agent.sh

venv:
	@~/.local/bin/uv venv --python 3.12 $(REPO_ROOT)/venvs/agent
	@~/.local/bin/uv pip install --python $(REPO_ROOT)/venvs/agent/bin/python \
	  --find-links $(REPO_ROOT)/server/wheels \
	  -e $(REPO_ROOT)/contracts \
	  -e $(REPO_ROOT)/server

status:
	@echo "=== TRID3NT Local service status ==="
	@printf "minio  (9000): " && \
	  if curl -sf http://127.0.0.1:9000/minio/health/live > /dev/null 2>&1; \
	  then echo "OK"; else echo "FAIL"; fi
	@printf "agent  (8766): " && \
	  if curl -sf http://127.0.0.1:8766/api/telemetry/summary > /dev/null 2>&1; \
	  then echo "OK"; else echo "FAIL"; fi
	@printf "ollama (11434): " && \
	  if curl -sf http://127.0.0.1:11434/api/tags > /dev/null 2>&1; \
	  then echo "OK (optional local LLM)"; else echo "not running (optional)"; fi

stop:
	@echo "=== stopping TRID3NT Local services ==="
	@if [ -f $(RUN_DIR)/minio.pid ]; then \
	  PID=$$(cat $(RUN_DIR)/minio.pid); \
	  if kill -0 $$PID 2>/dev/null; then \
	    echo "stopping minio (pid $$PID)"; kill $$PID; \
	  else echo "minio not running (stale pid $$PID)"; fi; \
	  rm -f $(RUN_DIR)/minio.pid; \
	else echo "no minio.pid found"; fi
	@if [ -f $(RUN_DIR)/agent.pid ]; then \
	  PID=$$(cat $(RUN_DIR)/agent.pid); \
	  if kill -0 $$PID 2>/dev/null; then \
	    echo "stopping agent (pid $$PID)"; kill $$PID; \
	  else echo "agent not running (stale pid $$PID)"; fi; \
	  rm -f $(RUN_DIR)/agent.pid; \
	else echo "no agent.pid found"; fi
	@echo "done"
