#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Simply Energy Home — Installeur universel
#
# Marche sur :
#   - Raspberry Pi OS (Bookworm/Bullseye) — ARM64/ARMHF
#   - Debian 12 / 11 — x86_64
#   - Ubuntu 22.04 / 24.04 — x86_64
#   - Armbian (Orange Pi, Khadas, Rock Pi…) — ARM64
#
# Installe :
#   - Docker + docker compose plugin
#   - Services hôte SEH (network setup, mDNS seh.local) — sprint 11
#   - L'app SEH en conteneur Docker
#   - Configuration auto pour démarrage au boot
#
# Usage (depuis n'importe quelle machine fraîchement installée) :
#   curl -fsSL https://raw.githubusercontent.com/<USER>/seh/main/scripts/install-seh.sh | sudo bash
#
# Variables d'env supportées :
#   SEH_VERSION       (default: latest)        — tag GitHub release
#   SEH_BRANCH        (default: main)          — branche pour fetch des fichiers
#   SEH_DATA_DIR      (default: /var/lib/seh)  — où stocker la DB et la config
#   SEH_PORT          (default: 8000)          — port HTTP de l'app
#   SEH_SKIP_NETWORK  (default: 0)             — 1 = ne pas installer le hotspot
#   SEH_SKIP_DOCKER   (default: 0)             — 1 = suppose Docker déjà installé
#   SEH_NONINTERACTIVE (default: 0)            — 1 = pas de prompt
#   SEH_REPO          (default: USER/seh)      — repo GitHub source
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Variables ────────────────────────────────────────────────────────
SEH_VERSION="${SEH_VERSION:-latest}"
SEH_BRANCH="${SEH_BRANCH:-main}"
SEH_DATA_DIR="${SEH_DATA_DIR:-/var/lib/seh}"
SEH_PORT="${SEH_PORT:-8000}"
SEH_SKIP_NETWORK="${SEH_SKIP_NETWORK:-0}"
SEH_SKIP_DOCKER="${SEH_SKIP_DOCKER:-0}"
SEH_NONINTERACTIVE="${SEH_NONINTERACTIVE:-0}"
SEH_REPO="${SEH_REPO:-homeassistantgazzzzton-droid/seh}"

INSTALL_DIR="/opt/seh"
LOG_FILE="/var/log/seh-install.log"

# ── Couleurs ─────────────────────────────────────────────────────────
if [ -t 1 ]; then
  G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[1;34m'; M='\033[1;35m'; N='\033[0m'
else
  G=''; Y=''; R=''; B=''; M=''; N=''
fi

log()    { printf "${B}[seh]${N} %s\n" "$*" | tee -a "$LOG_FILE"; }
ok()     { printf "${G}[ok]${N}  %s\n" "$*" | tee -a "$LOG_FILE"; }
warn()   { printf "${Y}[!]${N}   %s\n" "$*" | tee -a "$LOG_FILE"; }
err()    { printf "${R}[err]${N} %s\n" "$*" | tee -a "$LOG_FILE" >&2; }
banner() { printf "\n${M}═══════════════════════════════════════════════════════════════${N}\n${M}  %s${N}\n${M}═══════════════════════════════════════════════════════════════${N}\n\n" "$*"; }

# ── Sanity ───────────────────────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
  err "Ce script doit être lancé en root."
  echo "  Réessayez avec : curl -fsSL ... | sudo bash"
  exit 1
fi

mkdir -p "$(dirname "$LOG_FILE")"
echo "── SEH install $(date -Iseconds) ──" >> "$LOG_FILE"

banner "Simply Energy Home — Installation"

# ── Détection système ────────────────────────────────────────────────
log "Détection du système…"

if [ ! -f /etc/os-release ]; then
  err "Système non identifié (pas de /etc/os-release). Distribution non supportée."
  exit 1
fi
. /etc/os-release
OS_ID="${ID:-unknown}"
ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m)"

log "OS    : $PRETTY_NAME"
log "Arch  : $ARCH"

# Vérification famille Debian (pi-os, debian, ubuntu, armbian)
case "$OS_ID" in
  debian|ubuntu|raspbian) : ;;
  *)
    if echo "${ID_LIKE:-}" | grep -qv debian; then
      err "Distribution '$OS_ID' non supportée. Supportées : Debian, Ubuntu, Raspberry Pi OS, Armbian."
      exit 1
    fi
    ;;
esac

# Détection plateforme matérielle
PLATFORM="generic"
if [ -f /sys/firmware/devicetree/base/model ]; then
  MODEL="$(tr -d '\0' < /sys/firmware/devicetree/base/model)"
  log "Modèle: $MODEL"
  if echo "$MODEL" | grep -qi "raspberry"; then
    PLATFORM="raspberry-pi"
  elif echo "$MODEL" | grep -qiE "(orange pi|khadas|rock pi|nanopi)"; then
    PLATFORM="armbian"
  fi
elif [ "$ARCH" = "amd64" ] || [ "$ARCH" = "x86_64" ]; then
  PLATFORM="x86"
fi
log "Plateforme: $PLATFORM"

# ── Confirmation ─────────────────────────────────────────────────────
if [ "$SEH_NONINTERACTIVE" != "1" ] && [ -t 0 ]; then
  echo
  echo "Ce script va :"
  echo "  • Installer Docker et les paquets système requis"
  echo "  • Télécharger Simply Energy Home dans $INSTALL_DIR"
  if [ "$SEH_SKIP_NETWORK" != "1" ] && [ "$PLATFORM" != "x86" ]; then
    echo "  • Installer le service de provisioning wifi (hotspot 1er boot)"
  fi
  echo "  • Configurer le démarrage automatique"
  echo "  • Stocker les données dans $SEH_DATA_DIR"
  echo
  read -r -p "Continuer ? [O/n] " ans
  case "$ans" in
    [nN]*) log "Annulation."; exit 0 ;;
  esac
fi

# ── Helpers réseau ───────────────────────────────────────────────────
github_archive() {
  if [ "$SEH_VERSION" = "latest" ]; then
    echo "https://github.com/${SEH_REPO}/archive/refs/heads/${SEH_BRANCH}.tar.gz"
  else
    echo "https://github.com/${SEH_REPO}/archive/refs/tags/${SEH_VERSION}.tar.gz"
  fi
}

# ── 1. Paquets de base ───────────────────────────────────────────────
banner "1/5 — Paquets de base"

export DEBIAN_FRONTEND=noninteractive
log "Mise à jour de la liste des paquets…"
apt-get update -qq

BASE_PKGS=(curl ca-certificates gnupg lsb-release tar xz-utils avahi-daemon)
log "Installation : ${BASE_PKGS[*]}"
apt-get install -y --no-install-recommends "${BASE_PKGS[@]}"

# ── 2. Docker ────────────────────────────────────────────────────────
banner "2/5 — Docker"

if [ "$SEH_SKIP_DOCKER" = "1" ]; then
  log "SEH_SKIP_DOCKER=1 — installation Docker ignorée"
  command -v docker >/dev/null 2>&1 || { err "docker introuvable"; exit 1; }
elif command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  ok "Docker + compose plugin déjà installés"
else
  log "Installation Docker via le script officiel…"
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
  ok "Docker installé"
fi

# Test rapide
if ! docker info >/dev/null 2>&1; then
  err "Docker ne répond pas. Vérifiez l'installation et relancez."
  exit 1
fi

# ── 3. Téléchargement code SEH ──────────────────────────────────────
banner "3/5 — Téléchargement Simply Energy Home"

log "Création de $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$SEH_DATA_DIR"
chmod 0750 "$SEH_DATA_DIR"

ARCHIVE_URL="$(github_archive)"
log "Source: $ARCHIVE_URL"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

curl -fsSL --retry 3 --retry-delay 2 "$ARCHIVE_URL" -o "$TMPDIR/seh.tar.gz" || {
  err "Téléchargement échoué. Vérifiez SEH_REPO / SEH_VERSION / votre connexion."
  exit 1
}

log "Extraction…"
tar -xzf "$TMPDIR/seh.tar.gz" -C "$TMPDIR"
SRC_DIR="$(ls -d "$TMPDIR"/seh-* 2>/dev/null | head -n1)"
if [ -z "$SRC_DIR" ]; then
  err "Archive invalide (pas de dossier seh-*)"
  exit 1
fi

# Copie des fichiers (on évite rsync pour pas dépendre de plus)
cp -a "$SRC_DIR"/. "$INSTALL_DIR/"
ok "Code copié dans $INSTALL_DIR"

# ── 4. Services hôte (sprint 11) ────────────────────────────────────
if [ "$SEH_SKIP_NETWORK" = "1" ]; then
  banner "4/5 — Services hôte (IGNORÉ)"
  log "SEH_SKIP_NETWORK=1 — pas d'installation du hotspot wifi"
elif [ "$PLATFORM" = "x86" ]; then
  banner "4/5 — Services hôte (mDNS uniquement)"
  log "Plateforme x86 — pas de hotspot wifi (typiquement branché en ethernet)"
  log "Configuration mDNS → seh.local"
  hostnamectl set-hostname seh
  if ! grep -q "127.0.1.1.*seh" /etc/hosts; then
    sed -i '/127\.0\.1\.1/d' /etc/hosts
    echo "127.0.1.1   seh seh.local" >> /etc/hosts
  fi
  systemctl enable --now avahi-daemon
  ok "mDNS configuré"
else
  banner "4/5 — Services hôte (hotspot + mDNS)"
  if [ -f "$INSTALL_DIR/host-services/install.sh" ]; then
    log "Installation services réseau via host-services/install.sh"
    bash "$INSTALL_DIR/host-services/install.sh"
  else
    warn "host-services/install.sh introuvable — services réseau ignorés"
    warn "(le sprint 11 doit être présent dans le repo)"
  fi
fi

# ── 5. Conteneur SEH ────────────────────────────────────────────────
banner "5/5 — Conteneur Simply Energy Home"

# Génère un docker-compose.yml standardisé si pas présent
COMPOSE_FILE="$INSTALL_DIR/docker-compose.yml"
if [ ! -f "$COMPOSE_FILE" ]; then
  warn "docker-compose.yml absent du repo — génération d'une version par défaut"
  cat > "$COMPOSE_FILE" <<EOF
services:
  seh-app:
    image: ghcr.io/${SEH_REPO}:${SEH_VERSION}
    container_name: seh-app
    restart: unless-stopped
    ports:
      - "${SEH_PORT}:8000"
    volumes:
      - ${SEH_DATA_DIR}:/data
    environment:
      - DATA_DIR=/data
      - LOG_LEVEL=INFO
EOF
fi

# Variables d'env injectées dans compose
cat > "$INSTALL_DIR/.env" <<EOF
SEH_DATA_DIR=$SEH_DATA_DIR
SEH_PORT=$SEH_PORT
SEH_VERSION=$SEH_VERSION
EOF

log "Démarrage du conteneur…"
cd "$INSTALL_DIR"
docker compose pull --quiet || warn "docker compose pull échoué (image peut-être pas encore publiée)"
docker compose up -d

# ── Service systemd qui re-démarre Docker au boot ───────────────────
# Docker Compose s'occupe déjà de relancer le conteneur via restart:unless-stopped,
# mais on s'assure que docker.service est enable.
systemctl enable docker

# ── Récap ────────────────────────────────────────────────────────────
banner "Installation terminée 🎉"

# Tente de récupérer une IP utile à afficher
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

cat <<EOF
${G}Simply Energy Home est installé et démarré !${N}

Accès au tableau de bord :
EOF
if [ -n "$IP" ]; then
  echo "  → http://${IP}:${SEH_PORT}"
fi
echo "  → http://seh.local:${SEH_PORT}    (si votre OS supporte mDNS — Mac/iOS/Windows 10+/Linux)"
cat <<EOF

Premiers pas :
  • Au premier accès, le wizard d'onboarding s'ouvre
  • Créez votre compte administrateur
  • Configurez vos onduleurs/batteries

Logs en direct :
  cd $INSTALL_DIR && docker compose logs -f

Mise à jour ultérieure :
  cd $INSTALL_DIR && docker compose pull && docker compose up -d

Désinstallation :
  cd $INSTALL_DIR && docker compose down
  rm -rf $INSTALL_DIR
  # (les données restent dans $SEH_DATA_DIR)

Support :
  https://github.com/${SEH_REPO}/issues

EOF
exit 0
