#!/usr/bin/env bash
# Installe « Transcription Vidéo » comme un logiciel Windows (depuis WSL) :
# icône sur le Bureau et dans le menu Démarrer. Un double-clic affiche un écran
# de démarrage (sans console), démarre la stack Docker puis ouvre l'interface
# dans une fenêtre d'application, sans onglets ni barre d'adresse.
set -euo pipefail
cd "$(dirname "$0")/.."

PS=/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe
[ -x "$PS" ] || { echo "Windows introuvable : ce script s'exécute depuis WSL." >&2; exit 1; }
ps() { (cd /mnt/c && "$PS" -NoProfile -Command "$1" | tr -d '\r'); }

# Distribution qui contient le dépôt : la courante, sinon celle par défaut (« * »).
[ -n "${WSL_DISTRO_NAME:-}" ] || WSL_DISTRO_NAME=$(/mnt/c/Windows/System32/wsl.exe -l -v \
    | iconv -f utf-16le -t utf-8 | tr -d '\r' | awk '$1 == "*" {print $2}')
[ -n "$WSL_DISTRO_NAME" ] || { echo "Distribution WSL introuvable." >&2; exit 1; }

# Fichiers côté Windows : lisibles même quand WSL est arrêté.
DEST=$(ps '$env:LOCALAPPDATA')'\TranscriptionVideo'
ps "New-Item -ItemType Directory -Force -Path '$DEST' | Out-Null"
DEST_WSL=$(wslpath "$DEST")
cp source/static/icon.ico source/static/icon-180.png "$DEST_WSL/"

# Écran de démarrage : modèle complété, en UTF-8 avec BOM pour mshta.
escape() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }
{
  printf '\xef\xbb\xbf'
  sed -e "s|__DISTRIBUTION__|$(escape "$WSL_DISTRO_NAME")|g" \
      -e "s|__DEPOT__|$(escape "$PWD")|g" \
      -e "s|__DOSSIER_WSL__|$(escape "$DEST_WSL")|g" \
      -e "s|__DOSSIER_JS__|$(escape "${DEST//\\/\\\\}")|g" \
      -e "s|__DOSSIER__|$(escape "$DEST")|g" \
      scripts/windows/TranscriptionVideo.hta
} > "$DEST_WSL/TranscriptionVideo.hta"

DESKTOP=$(ps '[Environment]::GetFolderPath("Desktop")')
PROGRAMS=$(ps '[Environment]::GetFolderPath("Programs")')
for LINK in "$DESKTOP\\Transcription Vidéo.lnk" "$PROGRAMS\\Transcription Vidéo.lnk"; do
  ps "
  \$s = (New-Object -ComObject WScript.Shell).CreateShortcut('$LINK')
  \$s.TargetPath = \"\$env:SystemRoot\\System32\\mshta.exe\"
  \$s.Arguments = '\"$DEST\\TranscriptionVideo.hta\"'
  \$s.WorkingDirectory = '$DEST'
  \$s.IconLocation = '$DEST\\icon.ico,0'
  \$s.Description = 'Transcription et compression de vidéos pour le RAG'
  \$s.Save()
  "
  echo "Raccourci : $LINK"
done
# Windows garde les icônes en cache : on le prévient du changement.
ps 'ie4uinit.exe -show' >/dev/null 2>&1 || true

echo "Installé dans : $DEST"
echo "  WSL : $WSL_DISTRO_NAME, dépôt $PWD"
