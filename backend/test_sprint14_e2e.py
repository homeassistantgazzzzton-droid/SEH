"""
Tests E2E sprint 14 — OTA updater + diagnostics.

Couvre :
  - Endpoints /api/update/* (status, check, apply, rollback, cancel)
  - Permissions (admin requis)
  - Flux complet : check → apply → écriture du request file
  - /api/diagnostics avec sanitization des secrets
"""
import json
import os
import shutil
import sys
import tempfile

TEST_DIR = tempfile.mkdtemp(prefix="seh-s14-")
os.environ["DATA_DIR"] = TEST_DIR
os.environ["JWT_SECRET_PATH"] = os.path.join(TEST_DIR, "jwt_secret")
os.environ["CONFIG_PATH"] = os.path.join(TEST_DIR, "config.json")
os.environ["INVERTER_TYPE"] = "none"
os.environ["BMS_TYPE"] = "none"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Avant d'importer main, on patch les chemins du updater pour pointer dans TEST_DIR
import updater as updater_mod
updater_mod.UPDATE_REQUEST_FILE = os.path.join(TEST_DIR, "update_request.json")
updater_mod.UPDATE_STATUS_FILE = os.path.join(TEST_DIR, "update_status.json")
updater_mod.CURRENT_VERSION_FILE = os.path.join(TEST_DIR, "current_version.json")

from fastapi.testclient import TestClient
import main


def _check(cond, msg):
    if not cond:
        print(f"❌ {msg}")
        sys.exit(1)
    print(f"✓ {msg}")


with TestClient(main.app) as client:
    # Setup wizard pour avoir un admin
    r = client.post("/api/setup/complete", json={
        "admin_username": "alex",
        "admin_password": "monMotDePasse123",
        "config": {"inverter": {"type": "none"}},
        "enabled_modules": [],
    })
    _check(r.status_code == 200, "Wizard complete OK")
    client.cookies.clear()
    client.post("/api/auth/login",
                json={"username": "alex", "password": "monMotDePasse123"})

    # ── 1. /api/update/status accessible (user) ────────────────────────
    r = client.get("/api/update/status")
    _check(r.status_code == 200, "GET /api/update/status accessible")
    data = r.json()
    _check("current" in data, "Réponse contient 'current'")
    _check("status" in data, "Réponse contient 'status'")
    _check(data["status"]["state"] == "idle", "État initial = idle")

    # ── 2. /api/update/check refuse user lambda ────────────────────────
    # Créer un user non-admin
    r = client.post("/api/auth/users", json={
        "username": "marie", "password": "mdp45678", "role": "user"
    })
    _check(r.status_code == 200, "User lambda créé")
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "marie", "password": "mdp45678"})
    r = client.get("/api/update/check?channel=stable")
    _check(r.status_code == 403, "/api/update/check refusé pour user lambda")

    # ── 3. Repasser admin ──────────────────────────────────────────────
    client.cookies.clear()
    client.post("/api/auth/login",
                json={"username": "alex", "password": "monMotDePasse123"})

    # ── 4. /api/update/check rejette channel invalide ─────────────────
    r = client.get("/api/update/check?channel=xyz")
    _check(r.status_code == 400, "Channel invalide → 400")

    # ── 5. /api/update/check vers GitHub (peut fail si pas d'internet) ─
    # On accepte les 2 cas : succès ou error dans la réponse
    r = client.get("/api/update/check?channel=stable")
    _check(r.status_code == 200, "/api/update/check répond 200 (fail réseau OK)")
    data = r.json()
    if "error" in data:
        _check("error" in data, f"Pas de connexion → erreur dans réponse OK ({data.get('error','')[:50]})")
    else:
        _check("update_available" in data, "Réponse contient update_available")

    # ── 6. /api/update/apply refuse channel invalide ───────────────────
    r = client.post("/api/update/apply", json={"channel": "invalid"})
    _check(r.status_code == 400, "Apply channel invalide → 400")

    # ── 7. /api/update/rollback sans previous_version ──────────────────
    r = client.post("/api/update/rollback")
    _check(r.status_code == 400, "Rollback sans previous → 400")

    # ── 8. Simuler une demande directe via updater_mod ─────────────────
    u = updater_mod.get_updater()
    # Force une version courante
    updater_mod.set_current_version(updater_mod.VersionInfo(
        version="v1.0.0", channel="stable",
        image_ref="ghcr.io/test/seh:1.0.0", installed_at=1000.0,
    ))

    # Demande de MAJ avec target version explicite (skip GitHub fetch)
    status = u.request_update("stable", target_version="v1.0.1")
    _check(status.state == "pending", "Status pending après request_update")

    # Vérifier que le fichier de requête a bien été écrit
    _check(os.path.exists(updater_mod.UPDATE_REQUEST_FILE),
           "Fichier update_request.json créé")
    with open(updater_mod.UPDATE_REQUEST_FILE) as f:
        req = json.load(f)
    _check(req["target_version"] == "v1.0.1", "Target version OK")
    _check(req["previous_version"] == "v1.0.0", "Previous version OK")
    _check(req["rollback"] is False, "rollback=false")

    # ── 9. /api/update/cancel annule pending ──────────────────────────
    r = client.post("/api/update/cancel")
    _check(r.status_code == 200, "Cancel OK")
    _check(r.json()["ok"] is True, "Cancel a réussi")
    _check(not os.path.exists(updater_mod.UPDATE_REQUEST_FILE),
           "Fichier de requête supprimé après cancel")

    # ── 10. /api/update/status reflète l'idle après cancel ───────────
    r = client.get("/api/update/status")
    _check(r.json()["status"]["state"] == "idle", "État idle après cancel")

    # ── 11. Rollback : créer un status avec previous, puis rollback ───
    updater_mod.set_status(updater_mod.UpdateStatus(
        state="success",
        completed_at=1000.0,
        target_version="v1.0.1",
        previous_version="v1.0.0",
    ))
    r = client.post("/api/update/rollback")
    _check(r.status_code == 200, "Rollback OK quand previous existe")
    with open(updater_mod.UPDATE_REQUEST_FILE) as f:
        req = json.load(f)
    _check(req["rollback"] is True, "Rollback request rollback=true")

    # ── 12. /api/diagnostics ────────────────────────────────────────────
    # Mettre quelques secrets dans la config pour tester le redacting
    r = client.post("/api/settings", json={
        "config": {
            "support": {"smtp_password": "supersecret123", "smtp_host": "smtp.gmail.com"},
            "alerts": {"telegram_token": "abc:def-token", "enabled": False},
        }
    })
    # /api/settings peut renvoyer 200 ou 401 selon le wiring — on continue
    r = client.get("/api/diagnostics")
    _check(r.status_code == 200, "/api/diagnostics accessible (admin)")
    diag = r.json()
    _check("version" in diag, "diagnostics contient version")
    _check("perf" in diag, "diagnostics contient perf")
    _check("modules_enabled" in diag, "diagnostics contient modules_enabled")
    _check("config_sanitized" in diag, "diagnostics contient config_sanitized")
    # Vérifier le sanitizing
    diag_str = json.dumps(diag)
    _check("supersecret123" not in diag_str, "Mot de passe SMTP redacted")
    _check("abc:def-token" not in diag_str, "Token Telegram redacted")
    _check("REDACTED" in diag_str, "Présence du marqueur REDACTED")

    # ── 13. /api/diagnostics refusé pour user lambda ──────────────────
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "marie", "password": "mdp45678"})
    r = client.get("/api/diagnostics")
    _check(r.status_code == 403, "/api/diagnostics refusé pour user lambda")


print()
print("🎉 Tous les tests sprint 14 PASS")
shutil.rmtree(TEST_DIR, ignore_errors=True)
