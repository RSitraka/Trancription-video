#!/usr/bin/env bash
# Lance le logiciel : démarre la stack Docker puis ouvre l'interface.
#
#   make launch                      dans un terminal (progression affichée)
#   lancer.sh --fenetre <fichier>    depuis l'icône Windows : écrit l'avancement
#                                    dans <fichier>, lu par l'écran de démarrage
#                                    (scripts/windows/TranscriptionVideo.hta)
set -uo pipefail
cd "$(dirname "$0")/.."

ETAT=
if [ "${1:-}" = "--fenetre" ]; then
  ETAT=${2:?chemin du fichier d\'état}
  : > "$ETAT"
  exec >> "${ETAT%.*}.log" 2>&1           # détail consultable depuis l'écran de démarrage
fi

# etape <code> <message> : avancement, pour l'écran de démarrage ou le terminal.
etape() {
  if [ -n "$ETAT" ]; then printf '%s|%s\n' "$1" "$2" >> "$ETAT"; else printf '%s\n' "$2"; fi
}
stop() {
  if [ -n "$ETAT" ]; then etape erreur "$1"; exit 1; fi
  printf '\n\033[31m✗ %s\033[0m\n' "$1"
  read -rp "Appuie sur Entrée pour fermer…" _
  exit 1
}

[ -n "$ETAT" ] || printf '\033[1mTranscription Vidéo\033[0m\n\n'

# Docker démarre avec WSL (systemd) : quelques secondes après un démarrage à froid.
etape docker "Démarrage de Docker…"
for _ in $(seq 1 90); do
  docker info >/dev/null 2>&1 && break
  sleep 1
done
docker info >/dev/null 2>&1 || stop "Docker ne répond pas. Dans Ubuntu : sudo systemctl start docker"

etape services "Démarrage des services…"
make --no-print-directory up || stop "Démarrage des services impossible."

# Worker GPU resté démarré : sous WSL, il perd parfois l'accès à la carte.
WORKER=$(docker compose ps -q worker 2>/dev/null)
if [ -n "$WORKER" ] && docker inspect "$WORKER" --format '{{json .HostConfig.DeviceRequests}}' | grep -q gpu \
   && ! docker exec "$WORKER" nvidia-smi -L >/dev/null 2>&1; then
  etape gpu "Reconnexion de la carte graphique…"
  docker restart "$WORKER" >/dev/null
fi

PORT=$(grep -E '^API_PORT=' .env 2>/dev/null | cut -d= -f2 | awk '{print $1}')
URL="http://localhost:${PORT:-8000}/"
etape interface "Préparation de l'interface…"
for _ in $(seq 1 120); do
  curl -fsS "${URL}health" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "${URL}health" >/dev/null 2>&1 || stop "L'interface ne répond pas (docker compose logs api)."

# Mode jeton (REQUIRE_TOKEN=true) : connexion automatique, le jeton passe dans le
# fragment de l'URL (#…), que le navigateur n'envoie pas au serveur ; l'interface
# l'efface dès son chargement. Mode local (défaut) : aucune connexion.
TOKEN=
if grep -qiE '^REQUIRE_TOKEN=(true|1|yes|on)\b' .env 2>/dev/null; then
  TOKEN=$(grep -E '^API_TOKEN=' .env 2>/dev/null | cut -d= -f2- | awk '{print $1}')
  TOKEN=${TOKEN:-$(docker compose exec -T api cat /data/api_token 2>/dev/null | tr -d '[:space:]')}
fi
OPEN="${URL}${TOKEN:+#jeton=$TOKEN}"

if [ -n "$ETAT" ]; then
  # L'écran de démarrage ouvre la fenêtre de l'application.
  etape pret "$OPEN"
  # Sous WSL, Docker s'arrête quand plus aucun processus wsl.exe ne tourne :
  # un seul gardien discret, partagé par tous les lancements, le maintient.
  exec flock -n /tmp/transcription-video.garde sleep infinity
fi

# Terminal : navigateur par défaut, Windows depuis WSL, sinon Linux.
CMD=/mnt/c/Windows/System32/cmd.exe
if [ -x "$CMD" ]; then
  (cd /mnt/c && "$CMD" /c start "" "$OPEN" >/dev/null 2>&1)
elif command -v xdg-open >/dev/null; then
  xdg-open "$OPEN" >/dev/null 2>&1
fi
printf '\n\033[32m✓ Ouvert dans le navigateur : %s\033[0m\n' "$URL"
echo "Laisse cette fenêtre ouverte (réduite) : sous WSL, Docker peut s'arrêter"
echo "quand plus aucune fenêtre Ubuntu n'est ouverte."
read -rp "Appuie sur Entrée pour fermer…" _
