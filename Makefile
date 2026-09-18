# Raccourcis Docker Compose. GPU détecté automatiquement ; forcer avec GPU=0 ou GPU=1.
GPU ?= $(shell command -v nvidia-smi >/dev/null 2>&1 || test -x /usr/lib/wsl/lib/nvidia-smi && echo 1 || echo 0)

ifeq ($(GPU),1)
COMPOSE := docker compose -f docker-compose.yml -f docker-compose.gpu.yml
else
COMPOSE := docker compose -f docker-compose.yml
endif

# Version déployée sur cette machine (make deploy) : celle que lancent l'icône et make up.
APP_TAG ?= $(shell cat .deploy/current 2>/dev/null || echo latest)
export APP_TAG

.PHONY: start stop up dev deploy rollback versions launch shortcut icons test test-all

# Premier démarrage : déploie la version actuelle si aucune ne l'a encore été.
start:
	@[ -f .deploy/current ] || $(MAKE) --no-print-directory deploy
	@$(MAKE) --no-print-directory up
	@port=$$(grep -E '^API_PORT=' .env 2>/dev/null | cut -d= -f2 | cut -d' ' -f1); \
	echo "Interface : http://localhost:$${port:-8000}"

stop:
	$(COMPOSE) down

# Démarre la version déployée, sans reconstruire : utilisé par l'icône du Bureau.
up:
	$(COMPOSE) up -d --no-build

# Mode développement : code du dépôt monté, API rechargée à chaque modification.
dev:
	$(COMPOSE) -f docker-compose.dev.yml up -d --no-build
	@echo "Mode développement : les modifications de source/ sont prises en compte."
	@echo "Worker : docker compose restart worker. Retour à la version déployée : make up"

# Tests, images versionnées, bascule et vérification ; retour automatique si échec.
deploy:
	@COMPOSE="$(COMPOSE)" ./scripts/deployer.sh deploy

# Retour à la version déployée précédente.
rollback:
	@COMPOSE="$(COMPOSE)" ./scripts/deployer.sh rollback

versions:
	@COMPOSE="$(COMPOSE)" ./scripts/deployer.sh versions

# Démarre la stack et ouvre l'interface dans le navigateur.
launch:
	./scripts/lancer.sh

# Installation Windows : icône sur le Bureau et dans le menu Démarrer (depuis WSL).
shortcut:
	./scripts/installer-raccourci.sh

# Régénère l'icône (onglet du navigateur et raccourci) à partir de scripts/make_icons.py.
icons:
	docker run --rm -u "$$(id -u)" -v "$$PWD:/w" -w /w transcription-video-audio:test \
		python scripts/make_icons.py

# Tests unitaires, API et sécurité, dans le conteneur (rien sur l'hôte).
# Le service de test n'utilise pas PostgreSQL : une valeur factice suffit si
# POSTGRES_PASSWORD n'est pas défini (Compose l'exige pour lire le fichier).
test:
	POSTGRES_PASSWORD=$${POSTGRES_PASSWORD:-inutilise-par-les-tests} \
	docker compose --profile test run --rm --build test pytest $(ARGS)

# Tous les tests : pytest, puis vérification de bout en bout sur la stack démarrée.
test-all: test
	./scripts/smoke_test.sh
