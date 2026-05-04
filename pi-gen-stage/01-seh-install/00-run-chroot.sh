#!/bin/bash -e
# ─────────────────────────────────────────────────────────────────────────────
# pi-gen sub-stage : installation de Simply Energy Home dans l'image
#
# Ce script s'exécute À L'INTÉRIEUR du rootfs en cours de construction
# (donc dans un chroot), pas sur l'hôte.
#
# Variables fournies par pi-gen :
#   $ROOTFS_DIR n'existe pas ici (on est déjà dans le chroot)
# ─────────────────────────────────────────────────────────────────────────────

echo "── Installation de Simply Energy Home dans l'image ──"

# 1. Désactive dhcpcd (RPi OS) au profit de NetworkManager
if systemctl is-enabled dhcpcd >/dev/null 2>&1; then
  systemctl disable dhcpcd || true
fi
# Active NetworkManager (déjà installé via 00-packages)
systemctl enable NetworkManager
systemctl enable avahi-daemon

# 2. Configurer hostname → seh
echo "seh" > /etc/hostname
sed -i '/127\.0\.1\.1/d' /etc/hosts
echo "127.0.1.1   seh seh.local" >> /etc/hosts

# 3. Installer Docker (depuis convenience script officiel)
echo "── Installation Docker ──"
curl -fsSL https://get.docker.com | sh
systemctl enable docker

# 4. Extraire l'archive SEH livrée par la CI
echo "── Extraction des sources SEH ──"
mkdir -p /opt/seh
tar -xzf /tmp/seh-source.tar.gz -C /opt/seh

# 5. Installer les services hôte (sprint 11 — hotspot + portail)
if [ -f /opt/seh/host-services/install.sh ]; then
  echo "── Installation des services hôte (hotspot + portail) ──"
  cd /opt/seh/host-services
  # On lance l'install mais sans déclencher apt-get update (déjà fait)
  # et sans systemctl restart (impossible en chroot)
  bash ./install.sh || true
  cd /
fi

# 6. Pré-créer les dossiers de données (volumes Docker)
mkdir -p /var/lib/seh
chmod 0750 /var/lib/seh

# 7. docker-compose.yml — on garde celui du repo si présent, sinon génère
if [ ! -f /opt/seh/docker-compose.yml ]; then
  cat > /opt/seh/docker-compose.yml <<'EOF'
services:
  seh-app:
    image: ghcr.io/homeassistantgazzzzton-droid/seh:latest
    container_name: seh-app
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - /var/lib/seh:/data
      - /opt/seh/frontend:/app/frontend:ro
    environment:
      - DATA_DIR=/data
      - LOG_LEVEL=INFO
EOF
fi

# 8. Service systemd qui démarre docker compose au boot
# (Docker s'occupe déjà du restart du conteneur, mais on s'assure que
# `docker compose up` est bien appelé au moins une fois après reboot
# pour les machines flashées qui n'auraient jamais lancé le compose)
cat > /etc/systemd/system/seh-compose.service <<'EOF'
[Unit]
Description=Simply Energy Home — Docker Compose stack
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/seh
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF
systemctl enable seh-compose.service

# 9. Pré-télécharger l'image Docker pour réduire le temps du 1er boot
# Désactivé pour l'instant car pi-gen tourne en chroot et n'a pas accès
# à un daemon Docker. Le 1er boot devra puller l'image.
# Si on veut pré-charger, il faut docker save / docker load lors du build CI
# AVANT cette étape, et copier le tar dans /opt/seh/seh-image.tar
if [ -f /opt/seh/seh-image.tar ]; then
  echo "── Pré-chargement de l'image Docker SEH ──"
  # Stocké pour chargement au 1er boot (pas dans le chroot)
  cat > /etc/systemd/system/seh-load-image.service <<'EOF'
[Unit]
Description=Pre-load SEH Docker image at first boot
ConditionPathExists=/opt/seh/seh-image.tar
After=docker.service
Requires=docker.service
Before=seh-compose.service

[Service]
Type=oneshot
ExecStart=/bin/bash -c '/usr/bin/docker load -i /opt/seh/seh-image.tar && rm -f /opt/seh/seh-image.tar'

[Install]
WantedBy=multi-user.target
EOF
  systemctl enable seh-load-image.service
fi

# 10. Bannière SSH personnalisée
cat > /etc/motd <<'EOF'

  ┌─────────────────────────────────────────────┐
  │  ⚡ Simply Energy Home                      │
  │                                             │
  │  Tableau de bord : http://seh.local         │
  │  Logs        : docker compose logs -f       │
  │                (depuis /opt/seh)            │
  │                                             │
  │  Identifiants par défaut SSH : seh/changemenow │
  │  ↳ CHANGEZ LE MOT DE PASSE AU 1er BOOT !    │
  └─────────────────────────────────────────────┘

EOF

# 11. Marqueur de version
echo "${IMG_NAME:-simply-energy-home}-$(date -u +%Y%m%d)" > /etc/seh-version

echo "── Installation SEH dans l'image terminée ──"
