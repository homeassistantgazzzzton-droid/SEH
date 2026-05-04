#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Test du script install-seh.sh dans un conteneur Docker.
#
# But : valider que le script fonctionne sur Debian 12 / Ubuntu 22.04 / 24.04
# sans avoir à flasher une carte SD ou installer une VM.
#
# Limitations du test en Docker :
#   - Docker dans Docker = on installe Docker mais on ne peut pas le lancer
#     (il faut soit DinD, soit on saute cette étape avec SEH_SKIP_DOCKER=1
#     et on vérifie juste que le reste se passe bien)
#   - Pas de hotspot wifi (impossible en conteneur sans privileged + iface wifi)
#     → on test avec SEH_SKIP_NETWORK=1
#
# Usage :
#   ./test-install-locally.sh             # teste sur Debian 12
#   ./test-install-locally.sh ubuntu:22.04
#   ./test-install-locally.sh debian:11
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

IMAGE="${1:-debian:12}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "═══════════════════════════════════════════════════════════════"
echo "  Test install-seh.sh dans $IMAGE"
echo "═══════════════════════════════════════════════════════════════"

# On monte le script dans un conteneur jetable
# SEH_SKIP_DOCKER=1 : on simule, Docker est déjà "installé"
# SEH_SKIP_NETWORK=1 : pas de hotspot dans un conteneur
# SEH_NONINTERACTIVE=1 : pas de prompt
docker run --rm \
  -v "$SCRIPT_DIR/install-seh.sh:/install-seh.sh:ro" \
  -e SEH_SKIP_DOCKER=1 \
  -e SEH_SKIP_NETWORK=1 \
  -e SEH_NONINTERACTIVE=1 \
  -e SEH_REPO=homeassistantgazzzzton-droid/seh \
  -e DEBIAN_FRONTEND=noninteractive \
  "$IMAGE" \
  bash -c '
    set -e
    apt-get update -qq
    # Installe juste les pré-requis bash + curl pour que le script tourne,
    # comme si l\''utilisateur tapait `curl ... | sudo bash`
    apt-get install -y --no-install-recommends curl ca-certificates
    # On simule la présence de docker pour le check SKIP_DOCKER
    mkdir -p /usr/bin
    cat > /usr/bin/docker <<EOF
#!/bin/bash
case "\$1" in
  info) exit 0 ;;
  compose) shift; case "\$1" in version) echo "compose v2.x"; exit 0;; pull|up) exit 0;; esac ;;
esac
exit 0
EOF
    chmod +x /usr/bin/docker
    bash /install-seh.sh
  '

echo
echo "✅ Test sur $IMAGE OK"
