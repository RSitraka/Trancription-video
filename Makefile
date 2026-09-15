# Raccourcis Docker Compose. GPU détecté automatiquement ; forcer avec GPU=0 ou GPU=1.
GPU ?= $(shell command -v nvidia-smi >/dev/null 2>&1 || test -x /usr/lib/wsl/lib/nvidia-smi && echo 1 || echo 0)

ifeq ($(GPU),1)
COMPOSE := docker compose -f docker-compose.yml -f docker-compose.gpu.yml
else
COMPOSE := docker compose
endif

.PHONY: start stop

start:
	$(COMPOSE) up -d --build
	@port=$$(grep -E '^API_PORT=' .env 2>/dev/null | cut -d= -f2 | cut -d' ' -f1); \
	echo "Interface : http://localhost:$${port:-8000}"

stop:
	$(COMPOSE) down
