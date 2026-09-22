#!/usr/bin/env bash
# Déploiement du logiciel sur cette machine.
#
#   deployer.sh deploy     tests, images versionnées, bascule, vérification
#   deployer.sh rollback   retour à la version précédente
#   deployer.sh versions   version en service, précédente, historique
#
# Appelé par make (deploy, rollback, versions), qui fournit COMPOSE (fichiers
# Compose CPU ou GPU). Les versions déployées sont notées dans .deploy/.
set -uo pipefail
cd "$(dirname "$0")/.."

COMPOSE=${COMPOSE:-docker compose}
IMAGE=transcription-video-audio
GARDER=3                                  # versions conservées pour revenir en arrière
mkdir -p .deploy
CURRENT=$(cat .deploy/current 2>/dev/null || true)
PREVIOUS=$(cat .deploy/previous 2>/dev/null || true)
PORT=$(grep -E '^API_PORT=' .env 2>/dev/null | cut -d= -f2 | awk '{print $1}')
URL="http://localhost:${PORT:-8000}"

ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
info() { printf '\033[1m→\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# Traitements en file ou en cours : un redémarrage du worker les interromprait.
active_jobs() {
  curl -fsS "$URL/jobs?limit=10000" 2>/dev/null \
    | grep -oE '"status":"(queued|downloading|running|extracting|transcribing|ocr|compressing)"' | wc -l
}

check_idle() {
  local n
  n=$(active_jobs)
  if [ "${n:-0}" -gt 0 ] && [ "${FORCE:-0}" != 1 ]; then
    die "$n traitement(s) en cours : attendre la fin, ou FORCE=1 (ils reprendront là où ils en étaient)."
  fi
}

# switch <version> : fait tourner la stack sur cette version et vérifie qu'elle répond.
switch() {
  info "Démarrage de la version $1"
  APP_TAG=$1 $COMPOSE up -d --no-build --remove-orphans || return 1
  for _ in $(seq 1 90); do
    if curl -fsS "$URL/health" 2>/dev/null | grep -q "\"version\":\"$1\""; then
      ok "Version $1 en service : $URL"
      return 0
    fi
    sleep 2
  done
  return 1
}

image_exists() {
  docker image inspect "$IMAGE:$1" >/dev/null 2>&1 || docker image inspect "$IMAGE:gpu-$1" >/dev/null 2>&1
}

deploy() {
  local commit version
  commit=$(git rev-parse --short HEAD 2>/dev/null || echo local)
  version="$(date +%Y.%m.%d-%H%M%S)-$commit"
  if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    version="$version-modifie"
    printf '\033[33m! Modifications non commitées : version marquée « modifie ».\033[0m\n'
    echo "  Conseil : git commit avant de déployer, pour savoir ce que contient chaque version."
  fi

  check_idle

  if [ "${SKIP_TESTS:-0}" != 1 ]; then
    info "Tests"
    make --no-print-directory test ARGS="-q" || die "Tests en échec : rien n'a été déployé."
    ok "Tests réussis"
  fi

  info "Construction des images $version"
  APP_TAG=$version $COMPOSE build api worker || die "Construction impossible : rien n'a été déployé."
  ok "Images construites"

  if ! switch "$version"; then
    printf '\033[31m✗ La version %s ne répond pas.\033[0m\n' "$version"
    if [ -n "$CURRENT" ] && image_exists "$CURRENT"; then
      info "Retour automatique à $CURRENT"
      switch "$CURRENT" || die "Retour impossible : voir docker compose logs api"
    fi
    die "Déploiement annulé (journal : docker compose logs api)."
  fi

  if [ -n "$CURRENT" ] && [ "$CURRENT" != "$version" ]; then echo "$CURRENT" > .deploy/previous; fi
  echo "$version" > .deploy/current
  printf '%s\t%s\t%s\n' "$(date '+%Y-%m-%d %H:%M')" "$version" "$(git log -1 --format=%s 2>/dev/null)" >> .deploy/historique
  cleanup
  echo
  ok "Déployé. L'icône du Bureau ouvre désormais la version $version."
  if [ -n "$CURRENT" ]; then echo "  En cas de problème : make rollback (retour à $CURRENT)"; fi
}

rollback() {
  [ -n "$PREVIOUS" ] || die "Aucune version précédente enregistrée."
  image_exists "$PREVIOUS" || die "Les images de $PREVIOUS ont été supprimées."
  check_idle
  switch "$PREVIOUS" || die "La version $PREVIOUS ne répond pas (docker compose logs api)."
  echo "$PREVIOUS" > .deploy/current
  echo "$CURRENT" > .deploy/previous
  printf '%s\t%s\t%s\n' "$(date '+%Y-%m-%d %H:%M')" "$PREVIOUS" "retour arrière depuis $CURRENT" >> .deploy/historique
  ok "Retour à $PREVIOUS effectué (make rollback à nouveau pour revenir à $CURRENT)."
}

# Supprime les images des versions anciennes, en gardant les GARDER dernières
# et la précédente.
cleanup() {
  local keep
  keep=$(cut -f2 .deploy/historique | awk '!seen[$0]++' | tail -n "$GARDER"; cat .deploy/current .deploy/previous 2>/dev/null)
  docker image ls "$IMAGE" --format '{{.Tag}}' | while read -r tag; do
    local version=${tag#gpu-}
    [[ "$version" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}- ]] || continue      # latest, gpu, test… : non gérées
    grep -qxF "$version" <<< "$keep" && continue
    docker image rm "$IMAGE:$tag" >/dev/null 2>&1 && echo "  image ancienne supprimée : $tag"
  done
}

versions() {
  echo "En service  : ${CURRENT:-aucune (make deploy)}"
  echo "Précédente  : ${PREVIOUS:-aucune}"
  if curl -fsS "$URL/health" >/dev/null 2>&1; then
    echo "Répond      : $(curl -fsS "$URL/health" | grep -oE '"version":"[^"]*"' | cut -d'"' -f4)"
  else
    echo "Répond      : service arrêté"
  fi
  if [ -f .deploy/historique ]; then
    echo; echo "Historique (derniers déploiements) :"
    tail -n 10 .deploy/historique | sed 's/^/  /'
  fi
}

case "${1:-}" in
  deploy) deploy ;;
  rollback) rollback ;;
  versions) versions ;;
  *) die "usage : $0 deploy|rollback|versions" ;;
esac
