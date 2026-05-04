#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Simply Energy Home — Update orchestrator (hôte)
#
# S'exécute sur l'hôte (pas dans le conteneur). Vérifie périodiquement la
# présence d'un fichier de demande de MAJ déposé par l'app dans le volume
# Docker partagé, et exécute la MAJ si présent.
#
# Architecture :
#   1. L'app web écrit /var/lib/seh/update_request.json
#   2. Ce script (lancé toutes les 60s par systemd timer) le détecte
#   3. Pull la nouvelle image Docker
#   4. Sauvegarde le tag actuel pour rollback
#   5. Redémarre la stack docker compose
#   6. Le watchdog (autre service) vérifiera la santé pendant 2 min
#
# Usage :
#   sudo ./seh-update.sh             # vérifie et applique si demande
#   sudo ./seh-update.sh --force     # ignore le verrou (debug)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

DATA_DIR="${SEH_DATA_DIR:-/var/lib/seh}"
INSTALL_DIR="${SEH_INSTALL_DIR:-/opt/seh}"
REQUEST_FILE="${DATA_DIR}/update_request.json"
STATUS_FILE="${DATA_DIR}/update_status.json"
VERSION_FILE="${DATA_DIR}/current_version.json"
LOCK_FILE="${DATA_DIR}/update.lock"
LOG_FILE="/var/log/seh-update.log"
MAX_PULL_TIMEOUT=300   # 5 min de timeout pour docker pull (Pi Zero 2W lent)

# ── Logging ──────────────────────────────────────────────────────────
log() {
  local msg="[$(date -Iseconds)] $*"
  echo "$msg" | tee -a "$LOG_FILE"
}

# ── Acquisition du lock ──────────────────────────────────────────────
if [ "${1:-}" != "--force" ]; then
  if ! mkdir "$LOCK_FILE.d" 2>/dev/null; then
    # Lock déjà acquis par un autre process
    exit 0
  fi
  trap 'rmdir "$LOCK_FILE.d" 2>/dev/null || true' EXIT
fi

# ── Si pas de demande, on sort silencieusement ───────────────────────
if [ ! -f "$REQUEST_FILE" ]; then
  exit 0
fi

log "── Demande de MAJ détectée ──"

# ── Helpers JSON (jq optionnel, fallback python3) ────────────────────
json_get() {
  local file="$1" key="$2"
  if command -v jq >/dev/null 2>&1; then
    jq -r ".$key // empty" "$file"
  else
    python3 -c "import json,sys; d=json.load(open('$file')); print(d.get('$key',''))"
  fi
}

json_set_status() {
  python3 - <<EOF
import json, time, os
status_file = "$STATUS_FILE"
try:
    s = json.load(open(status_file))
except Exception:
    s = {}
s.update($1)
tmp = status_file + ".tmp"
json.dump(s, open(tmp, "w"), indent=2)
os.replace(tmp, status_file)
EOF
}

set_version() {
  local version="$1" channel="$2" image_ref="$3"
  python3 - <<EOF
import json, os, time
data = {
    "version": "$version",
    "channel": "$channel",
    "image_ref": "$image_ref",
    "installed_at": time.time(),
}
tmp = "$VERSION_FILE.tmp"
json.dump(data, open(tmp, "w"), indent=2)
os.replace(tmp, "$VERSION_FILE")
EOF
}

# ── Lecture de la demande ────────────────────────────────────────────
TARGET_VERSION="$(json_get "$REQUEST_FILE" "target_version")"
TARGET_IMAGE="$(json_get "$REQUEST_FILE" "target_image_ref")"
PREVIOUS_VERSION="$(json_get "$REQUEST_FILE" "previous_version")"
PREVIOUS_IMAGE="$(json_get "$REQUEST_FILE" "previous_image_ref")"
CHANNEL="$(json_get "$REQUEST_FILE" "channel")"
IS_ROLLBACK="$(json_get "$REQUEST_FILE" "rollback")"

log "Cible: $TARGET_VERSION (channel=$CHANNEL, image=$TARGET_IMAGE, rollback=$IS_ROLLBACK)"

# ── Marquer comme started ────────────────────────────────────────────
json_set_status '{"state": "running", "started_at": '$(date +%s)'}'

# ── Backup tag : retag de l'image courante en "previous" ─────────────
# Permet rollback rapide même si on a perdu l'image originale
log "Backup de l'image courante…"
CURRENT_IMG="$(docker compose -f "$INSTALL_DIR/docker-compose.yml" config 2>/dev/null | awk '/image:/ {print $2; exit}')"
if [ -n "$CURRENT_IMG" ]; then
  if docker tag "$CURRENT_IMG" "seh-previous:rollback" 2>>"$LOG_FILE"; then
    log "✓ Image courante taguée seh-previous:rollback"
  else
    log "⚠ Impossible de tagger (image absente ?)"
  fi
fi

# ── Pull de la nouvelle image ────────────────────────────────────────
if [ "$IS_ROLLBACK" = "True" ] || [ "$IS_ROLLBACK" = "true" ]; then
  # Rollback : on utilise seh-previous:rollback s'il existe
  log "Rollback vers $PREVIOUS_VERSION"
  if ! docker image inspect seh-previous:rollback >/dev/null 2>&1; then
    log "❌ Pas d'image seh-previous disponible pour rollback"
    json_set_status '{"state": "failed", "completed_at": '$(date +%s)', "error": "Pas d image de rollback"}'
    rm -f "$REQUEST_FILE"
    exit 1
  fi
  TARGET_IMAGE="seh-previous:rollback"
else
  log "Pull image: $TARGET_IMAGE"
  if ! timeout "$MAX_PULL_TIMEOUT" docker pull "$TARGET_IMAGE" 2>&1 | tee -a "$LOG_FILE"; then
    log "❌ docker pull échoué (timeout ou erreur réseau)"
    json_set_status '{"state": "failed", "completed_at": '$(date +%s)', "error": "Echec docker pull"}'
    rm -f "$REQUEST_FILE"
    exit 1
  fi
fi

# ── Mise à jour du docker-compose.yml pour pointer vers la nouvelle image ──
log "Mise à jour docker-compose.yml"
COMPOSE_FILE="$INSTALL_DIR/docker-compose.yml"
COMPOSE_BAK="$INSTALL_DIR/docker-compose.yml.bak"
cp "$COMPOSE_FILE" "$COMPOSE_BAK"
# Remplace la première ligne `image:` par notre cible
python3 - <<EOF
import re
with open("$COMPOSE_FILE", "r") as f:
    content = f.read()
new = re.sub(r'(\s+image:\s*)[^\s]+', r'\g<1>$TARGET_IMAGE', content, count=1)
with open("$COMPOSE_FILE", "w") as f:
    f.write(new)
EOF

# ── Restart docker compose ───────────────────────────────────────────
log "docker compose up -d"
cd "$INSTALL_DIR"
if ! docker compose up -d 2>&1 | tee -a "$LOG_FILE"; then
  log "❌ docker compose up échoué — restauration compose.yml"
  cp "$COMPOSE_BAK" "$COMPOSE_FILE"
  docker compose up -d || true
  json_set_status '{"state": "failed", "completed_at": '$(date +%s)', "error": "Echec docker compose up"}'
  rm -f "$REQUEST_FILE"
  exit 1
fi

# ── Marquer success ──────────────────────────────────────────────────
log "✓ MAJ appliquée : $TARGET_VERSION"
set_version "$TARGET_VERSION" "$CHANNEL" "$TARGET_IMAGE"
json_set_status '{"state": "success", "completed_at": '$(date +%s)', "error": ""}'

# Le watchdog prendra le relais pour vérifier que tout marche pendant 2 min
# et déclenchera un rollback auto si /api/health KO

# ── Cleanup ──────────────────────────────────────────────────────────
rm -f "$REQUEST_FILE"
rm -f "$COMPOSE_BAK"

log "── MAJ terminée — watchdog actif pour 120s ──"
