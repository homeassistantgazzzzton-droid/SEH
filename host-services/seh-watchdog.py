#!/usr/bin/env python3
"""
Simply Energy Home — Update Watchdog (Sprint 14)

Service systemd qui surveille la santé de l'app après une MAJ.
Si /health renvoie KO pendant > 2 min après application d'une MAJ,
le watchdog déclenche un rollback automatique.

Architecture :
  - Tourne en permanence en arrière-plan
  - Lit /var/lib/seh/update_status.json toutes les 15 secondes
  - Si state="success" et completed_at > now - 120s → on est en "fenêtre de surveillance"
  - Pendant cette fenêtre :
      curl http://localhost:8000/health toutes les 15s
      Si 4 échecs consécutifs (60s) → rollback
  - Après 2 min sans incident → marque "stable", arrête de surveiller

Cette logique compense le fait que docker compose lui-même ne sait pas si l'app
est fonctionnellement OK (il sait juste qu'elle est démarrée).
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger("seh-watchdog")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DATA_DIR = os.environ.get("SEH_DATA_DIR", "/var/lib/seh")
INSTALL_DIR = os.environ.get("SEH_INSTALL_DIR", "/opt/seh")
STATUS_FILE = f"{DATA_DIR}/update_status.json"
HEALTH_URL = os.environ.get("SEH_HEALTH_URL", "http://localhost:8000/health")
WATCH_DURATION = 120        # 2 min de fenêtre après MAJ
CHECK_INTERVAL = 15         # check toutes les 15s
MAX_FAILURES = 4            # 4 échecs = rollback (60s sans réponse)
HEALTH_TIMEOUT = 5

_stop = False


def handle_stop(*_):
    global _stop
    _stop = True
    logger.info("Signal d'arrêt reçu")


signal.signal(signal.SIGTERM, handle_stop)
signal.signal(signal.SIGINT, handle_stop)


def read_status() -> dict:
    try:
        with open(STATUS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_status(updates: dict):
    try:
        s = read_status()
        s.update(updates)
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, STATUS_FILE)
    except OSError as e:
        logger.warning("Impossible d'écrire status: %s", e)


def check_health() -> bool:
    try:
        req = urllib.request.Request(HEALTH_URL)
        with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT) as resp:
            if resp.status != 200:
                return False
            data = resp.read()
            # Accepte tout 200 — le contenu peut être minimal
            return True
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return False


def trigger_rollback():
    """
    Le watchdog ne fait pas le rollback lui-même.
    Il dépose un fichier de demande de rollback et seh-update.sh s'en chargera.
    """
    logger.warning("=== ROLLBACK AUTOMATIQUE ===")
    status = read_status()
    previous = status.get("previous_version", "")
    if not previous:
        logger.error("Pas de previous_version connu — rollback impossible")
        return False

    request_data = {
        "channel": "rollback",
        "target_version": previous,
        "target_image_ref": "",
        "previous_version": "(unknown after fail)",
        "previous_image_ref": "",
        "requested_at": time.time(),
        "rollback": True,
        "auto_rollback": True,
    }
    try:
        request_path = f"{DATA_DIR}/update_request.json"
        Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
        tmp = request_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(request_data, f, indent=2)
        os.replace(tmp, request_path)
    except OSError as e:
        logger.error("Impossible d'écrire rollback request: %s", e)
        return False

    write_status({
        "state": "rolling_back",
        "error": f"Rollback automatique : healthcheck KO {MAX_FAILURES * CHECK_INTERVAL}s",
    })

    # Lance immédiatement le script de MAJ pour ne pas attendre le timer
    try:
        subprocess.Popen(
            ["/usr/local/bin/seh-update.sh"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("seh-update.sh lancé pour exécuter le rollback")
    except OSError as e:
        logger.error("Impossible de lancer seh-update.sh: %s", e)
        return False

    return True


def watch_after_update(completed_at: float):
    """Boucle de surveillance pendant WATCH_DURATION secondes."""
    deadline = completed_at + WATCH_DURATION
    failures = 0
    successes = 0
    logger.info("Surveillance post-MAJ démarrée (jusqu'à %s)",
                time.strftime("%H:%M:%S", time.localtime(deadline)))

    while time.time() < deadline and not _stop:
        ok = check_health()
        if ok:
            failures = 0
            successes += 1
            if successes % 4 == 0:
                logger.info("Health OK (%ds restants dans la fenêtre)",
                            int(deadline - time.time()))
        else:
            failures += 1
            logger.warning("Health KO (%d/%d)", failures, MAX_FAILURES)
            if failures >= MAX_FAILURES:
                logger.error("Trop d'échecs consécutifs → rollback")
                trigger_rollback()
                return
        time.sleep(CHECK_INTERVAL)

    if not _stop:
        logger.info("Fenêtre de surveillance terminée — MAJ stable")
        write_status({"watchdog_passed": True})


def main():
    logger.info("Watchdog démarré (data=%s, health=%s)", DATA_DIR, HEALTH_URL)
    last_seen_completed_at = 0

    while not _stop:
        status = read_status()
        state = status.get("state", "")
        completed = status.get("completed_at", 0)

        # Détecter une MAJ tout juste réussie qu'on n'a pas encore surveillée
        if state == "success" and completed > last_seen_completed_at:
            elapsed = time.time() - completed
            if elapsed < WATCH_DURATION:
                last_seen_completed_at = completed
                watch_after_update(completed)
            else:
                # MAJ trop ancienne, on la marque comme déjà passée
                last_seen_completed_at = completed

        # Sleep court entre les checks d'état
        for _ in range(CHECK_INTERVAL):
            if _stop:
                break
            time.sleep(1)

    logger.info("Watchdog arrêté")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("Watchdog crash: %s", e)
        sys.exit(1)
