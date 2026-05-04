"""
Smart Energy Hub — Log Reader (Sprint 15)

Lit les logs Docker du conteneur principal + journalctl des services hôte.

Garde-fous de sécurité :
  - Whitelist stricte des sources : impossible de lire un log arbitraire
  - Pas de shell=True, jamais
  - Tronque à 500 lignes max (configurable jusqu'à 2000 pour /api/support/export)
  - Échappe les caractères de contrôle dans la sortie
  - Détecte/masque les patterns sensibles (tokens, mots de passe en clair)

Sources supportées :
  - "app"          → docker logs du conteneur SEH (paramétrable via env)
  - "network"      → journalctl de seh-network-setup.service
  - "update"       → journalctl de seh-update.service
  - "watchdog"     → journalctl de seh-watchdog.service

Si docker/journalctl absents (mode dev hors Pi), retourne un message explicite
au lieu de crasher.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Whitelist en dur — impossible d'accéder à autre chose
DOCKER_CONTAINER = os.environ.get("SEH_LOG_CONTAINER", "seh-app")

LOG_SOURCES = {
    "app": {
        "label": "Application",
        "type": "docker",
        "target": DOCKER_CONTAINER,
        "description": "Logs de l'application Simply Energy Home",
    },
    "network": {
        "label": "Provisionnement réseau",
        "type": "journal",
        "target": "seh-network-setup.service",
        "description": "Hotspot wifi + portail captif (sprint 11)",
    },
    "update": {
        "label": "Mises à jour",
        "type": "journal",
        "target": "seh-update.service",
        "description": "Orchestrateur des MAJ Docker",
    },
    "watchdog": {
        "label": "Watchdog",
        "type": "journal",
        "target": "seh-watchdog.service",
        "description": "Surveillance post-MAJ avec rollback auto",
    },
}

MAX_LINES = 500
MAX_LINES_EXPORT = 2000   # pour support export
DEFAULT_LINES = 200

# Patterns sensibles à masquer dans la sortie
SENSITIVE_PATTERNS = [
    # Bearer tokens, JWT, etc.
    (re.compile(r'(Bearer\s+)([A-Za-z0-9_.\-+/=]{20,})', re.I), r'\1[REDACTED]'),
    (re.compile(r'(api[_-]?key["\s:=]+)([A-Za-z0-9_.\-+/=]{16,})', re.I), r'\1[REDACTED]'),
    (re.compile(r'(token["\s:=]+["\'])([^"\']{16,})(["\'])', re.I), r'\1[REDACTED]\3'),
    # password=xxx
    (re.compile(r'(password["\s:=]+["\'])([^"\']{4,})(["\'])', re.I), r'\1[REDACTED]\3'),
    (re.compile(r'(password["\s:=]+)([^\s"\',;]{4,})', re.I), r'\1[REDACTED]'),
    # SMTP : "smtp_password": "xxx"
    (re.compile(r'(smtp_password["\s:=]+["\'])([^"\']*)(["\'])', re.I), r'\1[REDACTED]\3'),
    # JWT à 3 parties
    (re.compile(r'\beyJ[A-Za-z0-9_=\-]+\.eyJ[A-Za-z0-9_=\-]+\.[A-Za-z0-9_=\-]+\b'), '[JWT_REDACTED]'),
]


@dataclass
class LogResult:
    source: str
    lines: list[str]
    truncated: bool
    available: bool
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "lines": self.lines,
            "truncated": self.truncated,
            "available": self.available,
            "error": self.error,
            "count": len(self.lines),
        }


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers internes
# ═══════════════════════════════════════════════════════════════════════════

def _sanitize(text: str) -> str:
    """Masque les patterns sensibles. Préserve le formatage."""
    if not text:
        return text
    for pattern, replacement in SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _strip_ansi(text: str) -> str:
    """Retire les séquences ANSI couleur (mauvais pour l'UI web)."""
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)


def _safe_lines(raw: str, max_lines: int) -> tuple[list[str], bool]:
    """Découpe en lignes, applique sanitization, tronque."""
    raw = _strip_ansi(raw)
    raw = _sanitize(raw)
    all_lines = raw.splitlines()
    truncated = len(all_lines) > max_lines
    if truncated:
        all_lines = all_lines[-max_lines:]
    return all_lines, truncated


def _is_command_available(cmd: str) -> bool:
    """Test rapide si une commande système existe."""
    try:
        r = subprocess.run([cmd, "--version"], capture_output=True, timeout=2)
        return r.returncode == 0 or r.returncode == 1  # certains tools renvoient 1 sur --version
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


# ═══════════════════════════════════════════════════════════════════════════
#  Lecteurs spécifiques
# ═══════════════════════════════════════════════════════════════════════════

def _read_docker_logs(container: str, lines: int, since: Optional[str] = None) -> tuple[str, Optional[str]]:
    """Renvoie (output, error)."""
    if not _is_command_available("docker"):
        return "", "Commande 'docker' introuvable"
    args = ["docker", "logs", "--tail", str(lines)]
    if since:
        args += ["--since", since]
    args.append(container)
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            err = (r.stderr or "").strip()
            if "No such container" in err:
                return "", f"Conteneur '{container}' introuvable"
            return "", err[:300] or f"docker logs exit {r.returncode}"
        # docker mélange stdout et stderr — on fusionne
        return (r.stdout or "") + (r.stderr or ""), None
    except subprocess.TimeoutExpired:
        return "", "Timeout (10s) sur docker logs"
    except OSError as e:
        return "", f"Erreur subprocess: {e}"


def _read_journalctl(unit: str, lines: int, since: Optional[str] = None) -> tuple[str, Optional[str]]:
    if not _is_command_available("journalctl"):
        return "", "Commande 'journalctl' introuvable (système non-systemd ?)"
    args = ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"]
    if since:
        args += ["--since", since]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return "", (r.stderr or "").strip()[:300] or f"journalctl exit {r.returncode}"
        # Si l'unit n'existe pas, journalctl renvoie 0 + un message minimal
        if "No entries" in r.stdout or not r.stdout.strip():
            return "", f"Aucun log pour '{unit}' (service non installé ou pas encore démarré)"
        return r.stdout, None
    except subprocess.TimeoutExpired:
        return "", "Timeout (10s) sur journalctl"
    except OSError as e:
        return "", f"Erreur subprocess: {e}"


# ═══════════════════════════════════════════════════════════════════════════
#  API publique
# ═══════════════════════════════════════════════════════════════════════════

def list_sources() -> list[dict]:
    """Liste les sources de logs disponibles (whitelist)."""
    return [
        {"id": k, "label": v["label"], "type": v["type"], "description": v["description"]}
        for k, v in LOG_SOURCES.items()
    ]


def read_logs(source: str, lines: int = DEFAULT_LINES,
              since: Optional[str] = None,
              for_export: bool = False) -> LogResult:
    """
    Lit les logs d'une source whitelistée.
    `since` : valeur acceptée par docker/journalctl (ex: "1h", "2024-01-01")
    """
    if source not in LOG_SOURCES:
        return LogResult(source=source, lines=[], truncated=False, available=False,
                         error=f"Source inconnue : {source}")

    cap = MAX_LINES_EXPORT if for_export else MAX_LINES
    lines = max(1, min(cap, int(lines)))

    spec = LOG_SOURCES[source]
    if spec["type"] == "docker":
        raw, err = _read_docker_logs(spec["target"], lines, since)
    elif spec["type"] == "journal":
        raw, err = _read_journalctl(spec["target"], lines, since)
    else:
        return LogResult(source=source, lines=[], truncated=False, available=False,
                         error=f"Type de source inconnu : {spec['type']}")

    if err:
        return LogResult(source=source, lines=[], truncated=False,
                         available=False, error=err)

    all_lines, truncated = _safe_lines(raw, lines)
    return LogResult(source=source, lines=all_lines, truncated=truncated,
                     available=True, error=None)
