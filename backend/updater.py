"""
Smart Energy Hub — OTA Updater (Sprint 14)

Gestion des mises à jour Docker depuis l'app.

Architecture :
  - Le conteneur ne peut pas se restart lui-même proprement.
  - On utilise un mécanisme de "trigger file" : l'app écrit une demande de MAJ
    dans /data/update_request.json (volume partagé).
  - Un script hôte (seh-update.sh) lance par cron toutes les minutes vérifie
    ce fichier, fait le `docker compose pull && up -d` si présent, puis le
    supprime.
  - Le watchdog systemd (seh-watchdog.py) surveille la santé après MAJ.
    Si /api/health KO pendant 2 min après une MAJ → rollback auto.

Channels :
  - stable : tag "latest" sur ghcr.io
  - dev    : tag "main" sur ghcr.io (rebuild à chaque push main)

Endpoints (côté API, voir main.py) :
  GET  /api/update/status      → version actuelle, dernière MAJ tentée, statut
  GET  /api/update/check       → existe-t-il une nouvelle version ?
  POST /api/update/apply       → demande la MAJ (admin only)
  POST /api/update/rollback    → revient à l'ancien tag

Sécurité :
  - Endpoints réservés admin
  - Le SHA de l'image cible est vérifié avant swap (anti-tampering)
  - Si le watchdog détecte une régression, rollback auto sans intervention
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════════════════

UPDATE_REQUEST_FILE = "/data/update_request.json"
UPDATE_STATUS_FILE = "/data/update_status.json"
CURRENT_VERSION_FILE = "/data/current_version.json"

# Channel par défaut
DEFAULT_CHANNEL = "stable"

# Tag mapping
CHANNEL_TAGS = {
    "stable": "latest",
    "dev": "main",
}

# Repo GitHub (sera remplacé au déploiement par sed)
GITHUB_REPO = os.environ.get("SEH_REPO", "homeassistantgazzzzton-droid/seh")


# ═══════════════════════════════════════════════════════════════════════════
#  Modèles
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class VersionInfo:
    version: str = ""              # tag complet (ex: "v1.2.3" ou "main")
    channel: str = "stable"        # stable | dev
    image_digest: str = ""         # sha256:... (si connu)
    installed_at: float = 0.0      # timestamp unix
    image_ref: str = ""            # ghcr.io/user/seh:tag

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class UpdateStatus:
    state: str = "idle"            # idle | pending | running | success | failed | rolled_back
    requested_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    target_channel: str = ""
    target_version: str = ""
    error: str = ""
    previous_version: str = ""     # pour rollback

    def to_dict(self) -> dict:
        return asdict(self)


# ═══════════════════════════════════════════════════════════════════════════
#  GitHub Releases / Container Registry helpers
# ═══════════════════════════════════════════════════════════════════════════

def fetch_latest_release(channel: str = "stable", timeout: int = 10) -> Optional[dict]:
    """
    Récupère la dernière release publiée sur GitHub.
    Channel "stable" → release stable
    Channel "dev"    → dernière commit sur main (via /commits)
    """
    if channel == "stable":
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    elif channel == "dev":
        url = f"https://api.github.com/repos/{GITHUB_REPO}/commits/main"
    else:
        return None

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "SimplyEnergyHome-Updater",
            "Accept": "application/vnd.github+json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        logger.warning("GitHub release fetch HTTP %s: %s", e.code, url)
        return None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        logger.warning("GitHub release fetch error: %s", e)
        return None


def parse_release_to_version(release_data: dict, channel: str) -> Optional[VersionInfo]:
    """Parse la réponse GitHub API en VersionInfo."""
    if not release_data:
        return None
    if channel == "stable":
        tag = release_data.get("tag_name", "")
        if not tag:
            return None
        return VersionInfo(
            version=tag,
            channel="stable",
            image_ref=f"ghcr.io/{GITHUB_REPO}:{tag.lstrip('v')}",
        )
    if channel == "dev":
        sha = release_data.get("sha", "")[:7]
        return VersionInfo(
            version=f"main-{sha}" if sha else "main",
            channel="dev",
            image_ref=f"ghcr.io/{GITHUB_REPO}:main",
        )
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  Persistance
# ═══════════════════════════════════════════════════════════════════════════

def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_json(path: str, data: dict):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def get_current_version() -> VersionInfo:
    """Lit la version actuellement installée."""
    data = _read_json(CURRENT_VERSION_FILE)
    if data:
        return VersionInfo(**data)
    # Fallback : lit depuis variable d'env injectée par l'image Docker
    env_ver = os.environ.get("SEH_VERSION", "")
    env_channel = os.environ.get("SEH_CHANNEL", "stable")
    return VersionInfo(
        version=env_ver or "unknown",
        channel=env_channel,
        installed_at=0,
    )


def set_current_version(v: VersionInfo):
    v.installed_at = v.installed_at or time.time()
    _write_json(CURRENT_VERSION_FILE, v.to_dict())


def get_status() -> UpdateStatus:
    data = _read_json(UPDATE_STATUS_FILE)
    if data:
        return UpdateStatus(**{k: v for k, v in data.items() if k in UpdateStatus.__dataclass_fields__})
    return UpdateStatus()


def set_status(s: UpdateStatus):
    _write_json(UPDATE_STATUS_FILE, s.to_dict())


# ═══════════════════════════════════════════════════════════════════════════
#  Updater
# ═══════════════════════════════════════════════════════════════════════════

class Updater:
    """
    Encapsule la logique de mise à jour côté app (sans exécuter docker).
    Le vrai pull est fait par le script hôte qui lit UPDATE_REQUEST_FILE.
    """

    def __init__(self):
        self._last_check: Optional[VersionInfo] = None
        self._last_check_at: float = 0

    def check(self, channel: str = DEFAULT_CHANNEL, force: bool = False) -> dict:
        """
        Interroge GitHub pour la dernière version du channel.
        Cache 5 min sauf si force=True.
        """
        now = time.time()
        if not force and self._last_check and (now - self._last_check_at) < 300 \
           and self._last_check.channel == channel:
            return {
                "available": self._last_check.to_dict(),
                "current": get_current_version().to_dict(),
                "update_available": self._is_newer(self._last_check),
                "cached": True,
            }

        data = fetch_latest_release(channel)
        if not data:
            return {
                "available": None,
                "current": get_current_version().to_dict(),
                "update_available": False,
                "error": "Impossible de joindre GitHub. Vérifiez la connexion Internet.",
            }
        v = parse_release_to_version(data, channel)
        if not v:
            return {"error": "Format de release invalide", "current": get_current_version().to_dict()}

        self._last_check = v
        self._last_check_at = now
        return {
            "available": v.to_dict(),
            "current": get_current_version().to_dict(),
            "update_available": self._is_newer(v),
            "cached": False,
        }

    def _is_newer(self, candidate: VersionInfo) -> bool:
        cur = get_current_version()
        if cur.version == "unknown":
            return True
        # Comparaison stricte : différent = update dispo
        # (pour stable on pourrait faire du semver mais "différent du tag actuel" suffit)
        return candidate.version != cur.version

    def request_update(self, channel: str, target_version: Optional[str] = None) -> UpdateStatus:
        """
        Crée un fichier de demande de MAJ. Le script hôte le détectera et exécutera.
        """
        # Récupérer la version cible si pas fournie
        if not target_version:
            check_result = self.check(channel, force=True)
            avail = check_result.get("available")
            if not avail:
                raise RuntimeError("Impossible de déterminer la version cible. Vérifiez la connexion.")
            target_version = avail["version"]
            target_image_ref = avail["image_ref"]
        else:
            target_image_ref = f"ghcr.io/{GITHUB_REPO}:{target_version.lstrip('v')}"

        cur = get_current_version()

        # Status initial
        status = UpdateStatus(
            state="pending",
            requested_at=time.time(),
            target_channel=channel,
            target_version=target_version,
            previous_version=cur.version,
        )
        set_status(status)

        # Trigger file pour le script hôte
        request_data = {
            "channel": channel,
            "target_version": target_version,
            "target_image_ref": target_image_ref,
            "previous_version": cur.version,
            "previous_image_ref": cur.image_ref,
            "requested_at": status.requested_at,
            "rollback": False,
        }
        _write_json(UPDATE_REQUEST_FILE, request_data)
        logger.info("Update request créé : %s → %s (channel=%s)",
                    cur.version, target_version, channel)

        return status

    def request_rollback(self) -> UpdateStatus:
        """Demande un rollback vers la version précédente."""
        status = get_status()
        if not status.previous_version:
            raise RuntimeError("Pas de version précédente connue pour rollback.")

        request_data = {
            "channel": "rollback",
            "target_version": status.previous_version,
            "target_image_ref": "",  # le script hôte gardera l'image existante
            "previous_version": get_current_version().version,
            "previous_image_ref": get_current_version().image_ref,
            "requested_at": time.time(),
            "rollback": True,
        }
        _write_json(UPDATE_REQUEST_FILE, request_data)

        new_status = UpdateStatus(
            state="pending",
            requested_at=time.time(),
            target_channel="rollback",
            target_version=status.previous_version,
            previous_version=get_current_version().version,
        )
        set_status(new_status)
        logger.warning("Rollback demandé : %s → %s",
                       get_current_version().version, status.previous_version)
        return new_status

    def cancel_pending(self) -> bool:
        """Annule une MAJ pending si pas encore commencée."""
        status = get_status()
        if status.state != "pending":
            return False
        try:
            os.remove(UPDATE_REQUEST_FILE)
        except FileNotFoundError:
            pass
        status.state = "idle"
        set_status(status)
        return True


# ═══════════════════════════════════════════════════════════════════════════
#  Singleton
# ═══════════════════════════════════════════════════════════════════════════

_updater: Optional[Updater] = None


def get_updater() -> Updater:
    global _updater
    if _updater is None:
        _updater = Updater()
    return _updater
