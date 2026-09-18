#!/usr/bin/env bash
# Vérification de bout en bout : CLI, workers et API, sur un clip généré.
# Usage : ./scripts/smoke_test.sh          (modèle tiny, rapide)
#         WHISPER_MODEL=large-v3 ./scripts/smoke_test.sh
set -uo pipefail

cd "$(dirname "$0")/.."
PORT=$(grep -E '^API_PORT=' .env 2>/dev/null | cut -d= -f2 | awk '{print $1}')
PORT=${PORT:-8000}
MODEL=${WHISPER_MODEL:-tiny}
CLIP=/data/tmp/_smoke.mp4   # une source doit être sous MEDIA_PATH ou le dossier de travail

pass=0; fail=0
check() {  # check <libellé> <commande...>
  local label=$1; shift
  if "$@" >/tmp/_smoke.log 2>&1; then
    printf '  \033[32m✓\033[0m %s\n' "$label"; pass=$((pass + 1))
  else
    printf '  \033[31m✗\033[0m %s\n' "$label"; sed 's/^/      /' /tmp/_smoke.log | tail -5
    fail=$((fail + 1))
  fi
}
# Même version que la stack en service (make deploy).
export APP_TAG=${APP_TAG:-$(cat .deploy/current 2>/dev/null || echo latest)}
cli() { docker compose run --rm -T -e WHISPER_MODEL="$MODEL" -e CHUNK_DURATION=10 cli "$@"; }
# Jeton d'accès : API_TOKEN de .env, sinon celui que l'API a généré.
TOKEN=$(grep -E '^API_TOKEN=' .env 2>/dev/null | cut -d= -f2- | awk '{print $1}')
TOKEN=${TOKEN:-$(docker compose exec -T api cat /data/api_token 2>/dev/null | tr -d '[:space:]')}
export TOKEN
api() { curl -s --max-time 30 -H "Authorization: Bearer $TOKEN" "$@"; }
export -f api            # pour que les sous-shells `bash -c` la voient aussi

echo "Port API : $PORT — modèle : $MODEL"

echo; echo "1. Services"
check "conteneurs démarrés"  docker compose ps --status running --quiet
check "API en vie"           bash -c "api localhost:$PORT/health | grep -q '\"status\":\"ok\"'"
if grep -qiE '^REQUIRE_TOKEN=(true|1|yes|on)\b' .env 2>/dev/null; then
  check "jeton exigé (401 sans jeton)" \
      bash -c "curl -s -o /dev/null -w '%{http_code}' localhost:$PORT/jobs | grep -q 401"
fi
check "accès local accepté"  bash -c "api -o /dev/null -w '%{http_code}' localhost:$PORT/jobs | grep -q 200"
check "autre nom d'hôte refusé (403)" \
    bash -c "api -o /dev/null -w '%{http_code}' -H 'Host: malveillant.example' localhost:$PORT/jobs | grep -q 403"
check "requête d'un autre site refusée (403)" \
    bash -c "api -o /dev/null -w '%{http_code}' -X DELETE -H 'Origin: https://malveillant.example' 'localhost:$PORT/jobs?status=failed' | grep -q 403"
check "worker connecté"      bash -c "docker compose logs worker --tail 200 | grep -q 'celery@.*ready'"
check "postgres prêt"        docker compose exec -T postgres pg_isready -U transcription
check "ffmpeg dans l'image"  cli ffmpeg -version

echo; echo "2. Clip de test (20 s, mire + tonalité)"
check "génération" cli ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i "testsrc=size=640x360:rate=25:duration=20" \
    -f lavfi -i "sine=frequency=440:duration=20" \
    -c:v libx264 -preset ultrafast -c:a aac "$CLIP"

echo; echo "3. Ligne de commande"
check "info"       cli python main.py info "$CLIP"
check "transcribe" cli python main.py transcribe "$CLIP" --lang fr --formats srt,json,txt
check "sortie SRT créée"  cli test -f /data/out/_smoke/_smoke.srt
check "sortie JSON remplie" cli test -s /data/out/_smoke/_smoke.json
check "compress"   cli python main.py compress "$CLIP"
check "fichier compressé plus petit" \
    cli python -c "
import os, sys
src = os.path.getsize('$CLIP'); dst = os.path.getsize('/data/out/_smoke/_smoke_compressed.mp4')
print(f'{src} -> {dst} octets')
sys.exit(0 if dst < src else 1)"

echo; echo "4. API — job sur fichier local"
JOB=$(api -X POST "localhost:$PORT/jobs" -H 'Content-Type: application/json' \
      -d "{\"path\":\"$CLIP\",\"mode\":\"compress\"}")
ID=$(printf '%s' "$JOB" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
check "création du job" test -n "$ID"
STATUS=queued
for _ in $(seq 1 60); do
  STATUS=$(api "localhost:$PORT/jobs/$ID" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])' 2>/dev/null)
  [ "$STATUS" = done ] || [ "$STATUS" = failed ] && break
  sleep 2
done
check "traitement terminé ($STATUS)" test "$STATUS" = done
check "téléchargement du résultat" \
    bash -c "api -o /tmp/_smoke_out.mp4 -w '%{http_code}' localhost:$PORT/jobs/$ID/result.mp4 | grep -q 200"

echo; echo "5. API — upload chunké"
rm -rf /tmp/_smoke_parts && mkdir -p /tmp/_smoke_parts
split -n 3 -d /tmp/_smoke_out.mp4 /tmp/_smoke_parts/part_
SIZE=$(stat -c %s /tmp/_smoke_out.mp4)
UP=$(api -X POST "localhost:$PORT/jobs" -H 'Content-Type: application/json' \
     -d "{\"filename\":\"_smoke_upload.mp4\",\"size\":$SIZE,\"mode\":\"compress\"}")
UID_=$(printf '%s' "$UP" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
i=0; for f in /tmp/_smoke_parts/part_*; do
  api -X PUT "localhost:$PORT/jobs/$UID_/parts/$i" --data-binary @"$f" >/dev/null; i=$((i + 1))
done
check "3 morceaux reçus" \
    bash -c "api localhost:$PORT/jobs/$UID_/parts | grep -q '\"received\":\[0,1,2\]'"
check "assemblage" \
    bash -c "api -X POST localhost:$PORT/jobs/$UID_/complete | grep -q '\"status\":\"queued\"'"
STATUS=queued
for _ in $(seq 1 60); do
  STATUS=$(api "localhost:$PORT/jobs/$UID_" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])' 2>/dev/null)
  [ "$STATUS" = done ] || [ "$STATUS" = failed ] && break
  sleep 2
done
check "job d'upload terminé ($STATUS)" test "$STATUS" = done

echo; echo "6. Nettoyage"
for j in $ID $UID_; do [ -n "$j" ] && api -X DELETE "localhost:$PORT/jobs/$j" >/dev/null; done
check "fichiers temporaires supprimés" \
    cli sh -c 'rm -rf /data/out/_smoke* /data/tmp/_smoke* && ! ls /data/out/_smoke* /data/tmp/_smoke* >/dev/null 2>&1'
rm -rf /tmp/_smoke_parts /tmp/_smoke_out.mp4 /tmp/_smoke.log

echo
printf 'Résultat : \033[32m%d réussis\033[0m, ' "$pass"
if [ "$fail" -gt 0 ]; then printf '\033[31m%d échecs\033[0m\n' "$fail"; else printf '0 échec\n'; fi
exit $((fail > 0))
