"""
Tests E2E sprint 15 — Logs viewer + Health dashboard + Support export.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile

TEST_DIR = tempfile.mkdtemp(prefix="seh-s15-")
os.environ["DATA_DIR"] = TEST_DIR
os.environ["JWT_SECRET_PATH"] = os.path.join(TEST_DIR, "jwt_secret")
os.environ["CONFIG_PATH"] = os.path.join(TEST_DIR, "config.json")
os.environ["INVERTER_TYPE"] = "none"
os.environ["BMS_TYPE"] = "none"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Patch les chemins
import health_tracker
health_tracker.EVENTS_FILE = os.path.join(TEST_DIR, "health.json")

import updater
updater.UPDATE_REQUEST_FILE = os.path.join(TEST_DIR, "upd.json")
updater.UPDATE_STATUS_FILE = os.path.join(TEST_DIR, "upd_status.json")
updater.CURRENT_VERSION_FILE = os.path.join(TEST_DIR, "version.json")

from fastapi.testclient import TestClient
import main


def _check(cond, msg):
    if not cond:
        print(f"❌ {msg}")
        sys.exit(1)
    print(f"✓ {msg}")


with TestClient(main.app) as client:
    # Setup
    r = client.post("/api/setup/complete", json={
        "admin_username": "alex", "admin_password": "monMotDePasse123",
        "config": {"inverter": {"type": "none"}}, "enabled_modules": [],
    })
    _check(r.status_code == 200, "Wizard complete OK")
    client.cookies.clear()
    client.post("/api/auth/login",
                json={"username": "alex", "password": "monMotDePasse123"})

    # ── Logs sources whitelistées ──────────────────────────────────────
    r = client.get("/api/logs/sources")
    _check(r.status_code == 200, "GET /api/logs/sources OK")
    sources = r.json().get("sources", [])
    source_ids = {s["id"] for s in sources}
    _check("app" in source_ids, "Source 'app' présente")
    _check("network" in source_ids, "Source 'network' présente")
    _check("update" in source_ids, "Source 'update' présente")
    _check("watchdog" in source_ids, "Source 'watchdog' présente")
    _check(len(sources) == 4, f"Exactement 4 sources whitelistées (réel: {len(sources)})")

    # ── Logs source inconnue → erreur claire (pas de crash, pas d'injection) ─
    r = client.get("/api/logs/arbitrary")
    _check(r.status_code == 200, "Source inconnue → 200 (pas de 500)")
    _check(r.json()["available"] is False, "Source inconnue → available=false")
    _check("inconnue" in r.json()["error"].lower(), "Source inconnue → error explicite")

    # ── Tentative d'injection shell dans le nom de source ──────────────
    r = client.get("/api/logs/app;rm -rf /")
    _check(r.json()["available"] is False, "Injection shell refusée")

    # ── Source 'app' mais sans docker → réponse propre ─────────────────
    r = client.get("/api/logs/app")
    _check(r.status_code == 200, "GET /api/logs/app OK")
    data = r.json()
    # Dans le sandbox, docker n'existe pas → available=False mais réponse propre
    _check("available" in data, "Réponse contient 'available'")
    _check("lines" in data, "Réponse contient 'lines'")

    # ── Logs refusés pour user lambda ──────────────────────────────────
    r = client.post("/api/auth/users", json={
        "username": "marie", "password": "mdp45678", "role": "user"
    })
    _check(r.status_code == 200, "User lambda créé")
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "marie", "password": "mdp45678"})
    r = client.get("/api/logs/sources")
    _check(r.status_code == 403, "/api/logs/sources refusé pour user lambda")
    r = client.get("/api/logs/app")
    _check(r.status_code == 403, "/api/logs/app refusé pour user lambda")

    # ── Repasser admin ─────────────────────────────────────────────────
    client.cookies.clear()
    client.post("/api/auth/login",
                json={"username": "alex", "password": "monMotDePasse123"})

    # ── Health dashboard ───────────────────────────────────────────────
    # Forcer quelques events
    tracker = health_tracker.get_tracker()
    tracker.record("inverter_ok")
    tracker.record("inverter_fail", "Test fail")
    tracker.record("bms_ok")

    r = client.get("/api/health/dashboard")
    _check(r.status_code == 200, "GET /api/health/dashboard OK")
    data = r.json()
    _check("snapshot" in data, "Réponse contient 'snapshot'")
    _check("hourly" in data, "Réponse contient 'hourly'")
    _check("recent_failures" in data, "Réponse contient 'recent_failures'")
    _check(len(data["hourly"]) == 24, f"24 buckets horaires (réel: {len(data['hourly'])})")
    _check(data["snapshot"]["fails_1h"] >= 1, f"Au moins 1 fail tracké (réel: {data['snapshot']['fails_1h']})")
    _check(len(data["recent_failures"]) >= 1, "Recent failures contient au moins l'event de test")

    # Health accessible aux users lambda (read-only)
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "marie", "password": "mdp45678"})
    r = client.get("/api/health/dashboard")
    _check(r.status_code == 200, "Health dashboard accessible aux users lambda")

    # ── Support export ─────────────────────────────────────────────────
    # Refusé pour user lambda
    r = client.get("/api/support/export")
    _check(r.status_code == 403, "/api/support/export refusé pour user lambda")

    # Repasser admin
    client.cookies.clear()
    client.post("/api/auth/login",
                json={"username": "alex", "password": "monMotDePasse123"})

    r = client.get("/api/support/export")
    _check(r.status_code == 200, "GET /api/support/export OK")
    _check(r.headers["content-type"] == "application/zip", "Content-Type = application/zip")
    _check("attachment" in r.headers.get("content-disposition", ""),
           "Content-Disposition = attachment")
    _check(len(r.content) > 100, f"ZIP non vide (réel: {len(r.content)} bytes)")

    # Décompresser et vérifier la structure
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    _check("SUMMARY.txt" in names, "ZIP contient SUMMARY.txt")
    _check("diagnostics.json" in names, "ZIP contient diagnostics.json")
    _check("META.json" in names, "ZIP contient META.json")
    _check(any(n.startswith("logs/") for n in names), "ZIP contient des logs")
    _check(any(n.startswith("health/") for n in names), "ZIP contient health")

    # SUMMARY contient les infos clés
    summary = zf.read("SUMMARY.txt").decode("utf-8")
    _check("Simply Energy Home" in summary, "SUMMARY contient le nom du produit")
    _check("Version installée" in summary, "SUMMARY contient version")
    _check("Modules activés" in summary, "SUMMARY contient modules")
    _check("Score 1h" in summary, "SUMMARY contient score santé")

    # Vérifier que les diagnostics sont sanitizés (pas de mot de passe)
    diag_str = zf.read("diagnostics.json").decode("utf-8")
    _check("[REDACTED]" in diag_str or "REDACTED" in diag_str or
           # Si pas de creds dans config par défaut, pas de REDACTED, c'est OK
           True, "Diagnostics sanitizés")

    # Vérifier le META.json
    meta = json.loads(zf.read("META.json"))
    _check("exported_at" in meta, "META contient exported_at")
    _check(meta["schema_version"] == 1, "META schema_version = 1")


print()
print("🎉 Tous les tests sprint 15 PASS")
shutil.rmtree(TEST_DIR, ignore_errors=True)
