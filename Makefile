# Makefile -- TRID3NT Local runtime targets
REPO_ROOT := $(shell pwd)
SCRIPTS   := $(REPO_ROOT)/scripts
RUN_DIR   := $(REPO_ROOT)/run
LOG_DIR   := $(REPO_ROOT)/logs

.PHONY: binaries minio titiler agent web status stop

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
	@echo "agent venv not yet built"

web:
	@echo "web build not yet configured"

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
	@echo "done"
