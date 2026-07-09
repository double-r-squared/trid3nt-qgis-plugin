# Makefile -- TRID3NT Local runtime targets
REPO_ROOT := $(shell pwd)
SCRIPTS   := $(REPO_ROOT)/scripts
RUN_DIR   := $(REPO_ROOT)/run
LOG_DIR   := $(REPO_ROOT)/logs

.PHONY: binaries minio titiler agent venv web status stop

binaries:
	@bash $(SCRIPTS)/fetch_binaries.sh

minio:
	@mkdir -p $(LOG_DIR) $(RUN_DIR)
	@bash $(SCRIPTS)/start_minio.sh
	@bash $(SCRIPTS)/init_minio.sh

titiler:
	@mkdir -p $(LOG_DIR) $(RUN_DIR)
	@bash $(SCRIPTS)/start_titiler.sh

agent:
	@mkdir -p $(LOG_DIR) $(RUN_DIR)
	@bash $(SCRIPTS)/start_agent.sh

venv:
	@~/.local/bin/uv venv --python 3.12 $(REPO_ROOT)/venvs/agent
	@~/.local/bin/uv pip install --python $(REPO_ROOT)/venvs/agent/bin/python \
	  -e $(REPO_ROOT)/vendor/packages/contracts \
	  -e $(REPO_ROOT)/vendor/services/agent

web:
	@mkdir -p $(LOG_DIR) $(RUN_DIR)
	@if [ -f $(RUN_DIR)/web.pid ] && kill -0 $$(cat $(RUN_DIR)/web.pid) 2>/dev/null; then \
	  echo "web already running (pid $$(cat $(RUN_DIR)/web.pid))"; \
	else \
	  echo "starting vite dev server on :5173 ..."; \
	  setsid nohup sh -c 'cd $(REPO_ROOT)/vendor/web && VITE_DEPLOYMENT=local node_modules/.bin/vite --port 5173 --host 0.0.0.0' \
	    > $(LOG_DIR)/web.log 2>&1 & \
	  echo $$! > $(RUN_DIR)/web.pid; \
	  echo "web pid $$!  -- logs at $(LOG_DIR)/web.log"; \
	fi

status:
	@echo "=== TRID3NT Local service status ==="
	@printf "minio  (9000): " && \
	  if curl -sf http://127.0.0.1:9000/minio/health/live > /dev/null 2>&1; \
	  then echo "OK"; else echo "FAIL"; fi
	@printf "titiler (8080): " && \
	  if curl -sf http://127.0.0.1:8080/healthz > /dev/null 2>&1; \
	  then echo "OK"; else echo "FAIL"; fi
	@printf "ollama (11434): " && \
	  if curl -sf http://127.0.0.1:11434/api/tags > /dev/null 2>&1; \
	  then echo "OK"; else echo "FAIL"; fi

stop:
	@echo "=== stopping TRID3NT Local services ==="
	@if [ -f $(RUN_DIR)/minio.pid ]; then \
	  PID=$$(cat $(RUN_DIR)/minio.pid); \
	  if kill -0 $$PID 2>/dev/null; then \
	    echo "stopping minio (pid $$PID)"; kill $$PID; \
	  else echo "minio not running (stale pid $$PID)"; fi; \
	  rm -f $(RUN_DIR)/minio.pid; \
	else echo "no minio.pid found"; fi
	@if [ -f $(RUN_DIR)/titiler.pid ]; then \
	  PID=$$(cat $(RUN_DIR)/titiler.pid); \
	  if kill -0 $$PID 2>/dev/null; then \
	    echo "stopping titiler (pid $$PID)"; kill $$PID; \
	  else echo "titiler not running (stale pid $$PID)"; fi; \
	  rm -f $(RUN_DIR)/titiler.pid; \
	else echo "no titiler.pid found"; fi
	@if [ -f $(RUN_DIR)/agent.pid ]; then \
	  PID=$$(cat $(RUN_DIR)/agent.pid); \
	  if kill -0 $$PID 2>/dev/null; then \
	    echo "stopping agent (pid $$PID)"; kill $$PID; \
	  else echo "agent not running (stale pid $$PID)"; fi; \
	  rm -f $(RUN_DIR)/agent.pid; \
	else echo "no agent.pid found"; fi
	@echo "done"
