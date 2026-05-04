#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Simply Energy Home — Installeur services hôte (Sprint 11)
#
# Installe sur Raspberry Pi OS / Debian / Armbian / Ubuntu :
#   - Dépendances : NetworkManager, dnsmasq-base, avahi-daemon, iptables
#   - Service seh-network-setup (hotspot + captive portal au 1er boot)
#   - mDNS seh.local
#   - Page captive_portal.html
#
# Usage :
#   sudo ./install.sh                 # installation normale
#   sudo ./install.sh --uninstall     # nettoyage complet
#   sudo ./install.sh --dry-run       # affiche les actions sans exécuter
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

DRY_RUN=0
UNINSTALL=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Couleurs
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[1;34m'; N='\033[0m'

log() { printf "${B}[seh]${N} %s\n" "$*"; }
ok()  { printf "${G}[ok]${N}  %s\n" "$*"; }
warn(){ printf "${Y}[!]${N}   %s\n" "$*"; }
err() { printf "${R}[err]${N} %s\n" "$*" >&2; }

run() {
  if [ "$DRY_RUN" = "1" ]; then
    printf "  ${Y}[DRY]${N} %s\n" "$*"
  else
    eval "$@"
  fi
}

# ── Args ────────────────────────────────────────────────────────────
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help)
      grep '^#' "$0" | head -25
      exit 0
      ;;
    *) warn "Argument ignoré : $arg" ;;
  esac
done

# ── Sanity checks ────────────────────────────────────────────────────
if [ "$EUID" -ne 0 ] && [ "$DRY_RUN" = "0" ]; then
  err "Ce script doit être lancé en root (sudo)."
  exit 1
fi

if ! command -v systemctl >/dev/null; then
  err "systemd introuvable. Système non supporté."
  exit 1
fi

# ── Détection distribution ───────────────────────────────────────────
if [ -f /etc/os-release ]; then
  . /etc/os-release
  log "Système détecté : $PRETTY_NAME"
fi

# ── Mode désinstallation ─────────────────────────────────────────────
if [ "$UNINSTALL" = "1" ]; then
  log "Mode DÉSINSTALLATION"
  run "systemctl stop seh-network-setup.service || true"
  run "systemctl disable seh-network-setup.service || true"
  run "rm -f /etc/systemd/system/seh-network-setup.service"
  run "rm -f /usr/local/bin/seh-network-setup.py"
  run "rm -rf /usr/local/share/seh"
  run "rm -rf /var/lib/seh"
  # Supprimer la connexion hotspot si présente
  run "nmcli connection delete seh-hotspot 2>/dev/null || true"
  run "systemctl daemon-reload"
  ok "Désinstallation terminée."
  exit 0
fi

# ── Installation des dépendances ─────────────────────────────────────
log "Vérification des paquets requis…"

NEED_PKGS=()
for pkg in network-manager dnsmasq-base avahi-daemon iptables python3; do
  if ! dpkg -s "$pkg" >/dev/null 2>&1; then
    NEED_PKGS+=("$pkg")
  fi
done

if [ ${#NEED_PKGS[@]} -gt 0 ]; then
  log "Installation : ${NEED_PKGS[*]}"
  run "apt-get update -qq"
  run "DEBIAN_FRONTEND=noninteractive apt-get install -y ${NEED_PKGS[*]}"
else
  ok "Tous les paquets requis sont déjà installés."
fi

# Sur Raspberry Pi OS, dhcpcd peut entrer en conflit avec NetworkManager.
# On bascule en NetworkManager si c'est pas déjà fait.
if systemctl is-enabled dhcpcd >/dev/null 2>&1; then
  warn "dhcpcd est actif — désactivation au profit de NetworkManager."
  run "systemctl disable dhcpcd"
  run "systemctl stop dhcpcd || true"
fi

# Activer NetworkManager
log "Activation de NetworkManager…"
run "systemctl enable NetworkManager"
run "systemctl start NetworkManager || true"

# ── Configuration mDNS (avahi seh.local) ─────────────────────────────
log "Configuration mDNS → seh.local…"
run "hostnamectl set-hostname seh"
# /etc/hosts
if ! grep -q "127.0.1.1.*seh" /etc/hosts 2>/dev/null; then
  run "sed -i '/127.0.1.1/d' /etc/hosts"
  run "echo '127.0.1.1   seh seh.local' >> /etc/hosts"
fi
run "systemctl enable avahi-daemon"
run "systemctl restart avahi-daemon"

# ── Copie des fichiers ───────────────────────────────────────────────
log "Installation des fichiers…"

# Le script principal
if [ -f "$SCRIPT_DIR/seh-network-setup.py" ]; then
  run "install -m 0755 -D $SCRIPT_DIR/seh-network-setup.py /usr/local/bin/seh-network-setup.py"
else
  err "seh-network-setup.py introuvable dans $SCRIPT_DIR"
  exit 1
fi

# Le HTML du portail
if [ -f "$SCRIPT_DIR/captive_portal.html" ]; then
  run "install -m 0644 -D $SCRIPT_DIR/captive_portal.html /usr/local/share/seh/captive_portal.html"
else
  err "captive_portal.html introuvable"
  exit 1
fi

# L'unit systemd
if [ -f "$SCRIPT_DIR/seh-network-setup.service" ]; then
  run "install -m 0644 -D $SCRIPT_DIR/seh-network-setup.service /etc/systemd/system/seh-network-setup.service"
else
  err "seh-network-setup.service introuvable"
  exit 1
fi

# ─── Sprint 14 : update orchestrator + watchdog ─────────────────────
if [ -f "$SCRIPT_DIR/seh-update.sh" ]; then
  log "Installation update orchestrator + watchdog (sprint 14)…"
  run "install -m 0755 -D $SCRIPT_DIR/seh-update.sh /usr/local/bin/seh-update.sh"
  run "install -m 0755 -D $SCRIPT_DIR/seh-watchdog.py /usr/local/bin/seh-watchdog.py"
  run "install -m 0644 -D $SCRIPT_DIR/seh-update.service /etc/systemd/system/seh-update.service"
  run "install -m 0644 -D $SCRIPT_DIR/seh-update.timer /etc/systemd/system/seh-update.timer"
  run "install -m 0644 -D $SCRIPT_DIR/seh-watchdog.service /etc/systemd/system/seh-watchdog.service"
fi

# Dossier d'état
run "install -d -m 0755 /var/lib/seh"

# ── Activation du service ────────────────────────────────────────────
log "Activation du service systemd…"
run "systemctl daemon-reload"
run "systemctl enable seh-network-setup.service"

# Sprint 14 : update + watchdog
if [ -f /etc/systemd/system/seh-update.timer ]; then
  run "systemctl enable seh-update.timer"
  run "systemctl enable seh-watchdog.service"
fi

# ── Récap ────────────────────────────────────────────────────────────
echo
ok "Installation terminée !"
echo
echo "Prochaines étapes :"
echo "  1. Vérifier que NetworkManager gère wlan0 :"
echo "       nmcli device status"
echo "  2. Démarrer le service maintenant (optionnel) :"
echo "       sudo systemctl start seh-network-setup.service"
echo "  3. Voir les logs :"
echo "       sudo journalctl -u seh-network-setup.service -f"
echo "  4. Au prochain reboot, si pas de connectivité :"
echo "       Un wifi 'SimplyEnergyHome-XXXX' apparaîtra"
echo "       Connectez-vous (pas de mot de passe)"
echo "       La page de config s'ouvrira automatiquement"
echo
echo "Pour tester sans toucher au réseau (mode debug) :"
echo "  sudo /usr/local/bin/seh-network-setup.py --once --debug"
echo
