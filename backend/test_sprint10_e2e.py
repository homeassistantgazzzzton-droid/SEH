"""
Test end-to-end sprint 10 avec la vraie app main.py.
Vérifie le scénario complet :
  1. Premier démarrage → first_boot=true → wizard accessible
  2. POST /api/setup/complete → admin créé, first_boot=false
  3. /api/* protégées, /api/auth/login fonctionne
  4. /api/setup/* refusées si first_boot=false et pas admin
"""
import os
import shutil
import sys
import tempfile

# Setup environnement isolé
TEST_DIR = tempfile.mkdtemp(prefix="seh-test-")
os.environ["DATA_DIR"] = TEST_DIR
os.environ["JWT_SECRET_PATH"] = os.path.join(TEST_DIR, "jwt_secret")
os.environ["CONFIG_PATH"] = os.path.join(TEST_DIR, "config.json")
os.environ["INVERTER_TYPE"] = "none"
os.environ["BMS_TYPE"] = "none"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient
import main


def _check(cond, msg):
    if not cond:
        print(f"❌ {msg}")
        sys.exit(1)
    print(f"✓ {msg}")


# Le TestClient déclenche le lifespan
with TestClient(main.app) as client:
    # ── 1. Premier démarrage : first_boot=true, 0 user ──
    r = client.get("/api/setup/status")
    _check(r.status_code == 200, "GET /api/setup/status accessible")
    data = r.json()
    _check(data["first_boot"] is True, "first_boot=true au démarrage")
    _check(data["users_count"] == 0, "0 users au démarrage")
    _check(len(data["available_modules"]) >= 5, "Liste de modules disponibles présente")

    # ── 2. /api/status accessible en bootstrap (pas d'auth) ──
    r = client.get("/api/status")
    _check(r.status_code == 200, "/api/status accessible en bootstrap (sans auth)")

    # ── 3. /api/auth/me sans cookie → 401 ──
    # (auth requiert cookie quoi qu'il arrive sur cet endpoint)
    r = client.get("/api/auth/me")
    _check(r.status_code == 401, "/api/auth/me sans cookie → 401")

    # ── 4. POST /api/setup/complete crée l'admin ──
    r = client.post("/api/setup/complete", json={
        "admin_username": "alexis",
        "admin_password": "monMotDePasse123!",
        "config": {
            "inverter": {"type": "voltronic", "host": "192.168.1.100", "port": 8899},
            "finance": {"enabled": True, "import_price": 0.25, "export_price": 0.13},
        },
        "enabled_modules": ["voltronic", "finance"],
    })
    _check(r.status_code == 200, "POST /api/setup/complete OK")
    body = r.json()
    _check(body["bootstrap"] is True, "bootstrap=true (premier passage)")
    _check(body["admin_created"] is True, "admin_created=true")

    # ── 5. first_boot doit être false maintenant ──
    r = client.get("/api/setup/status")
    _check(r.json()["first_boot"] is False, "first_boot=false après complete")
    _check(r.json()["users_count"] == 1, "1 user après complete")

    # ── 6. /api/status maintenant ferme (pas de cookie) ──
    # Le TestClient garde les cookies entre requêtes par défaut. Il faut clear.
    client.cookies.clear()
    r = client.get("/api/status")
    _check(r.status_code == 401, "/api/status fermée après création admin")

    # ── 7. Login ──
    r = client.post("/api/auth/login",
                    json={"username": "alexis", "password": "monMotDePasse123!"})
    _check(r.status_code == 200, "Login admin OK")
    _check(r.json()["user"]["role"] == "admin", "Role admin retourné")

    # ── 8. /api/status maintenant accessible avec cookie ──
    r = client.get("/api/status")
    _check(r.status_code == 200, "/api/status accessible avec cookie admin")

    # ── 9. /api/setup/* refusé pour user lambda ──
    # Créer un user lambda
    r = client.post("/api/auth/users",
                    json={"username": "marie", "password": "motdepasse456", "role": "user"})
    _check(r.status_code == 200, "Admin crée un user lambda")

    # Login en lambda
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "marie", "password": "motdepasse456"})

    # /api/setup/scan_network refusé en user (first_boot=false + role=user)
    r = client.post("/api/setup/scan_network", json={"timeout": 0.5})
    _check(r.status_code == 403, "/api/setup/scan_network refusé pour user lambda")

    # /api/setup/reopen refusé pour user lambda
    r = client.post("/api/setup/reopen")
    _check(r.status_code == 403, "/api/setup/reopen refusé pour user lambda")

    # ── 10. Admin peut rouvrir le wizard ──
    client.cookies.clear()
    client.post("/api/auth/login",
                json={"username": "alexis", "password": "monMotDePasse123!"})
    r = client.post("/api/setup/reopen")
    _check(r.status_code == 200, "Admin peut rouvrir le wizard")
    r = client.get("/api/setup/status")
    _check(r.json()["first_boot"] is True, "first_boot=true après reopen")

    # ── 11. /api/support/send sans config SMTP → 503 (pas crash) ──
    client.cookies.clear()
    r = client.post("/api/support/send", json={
        "name": "Test", "email": "test@example.com",
        "subject": "Test", "body": "Message de test pour validation",
    })
    _check(r.status_code == 503, "/api/support/send sans config SMTP → 503 propre")

    # ── 12. Reset flag : créer fichier reset.flag, simuler restart ──
    # On le teste directement sur _db
    db = main._db
    flag_path = os.path.join(TEST_DIR, "reset.flag")
    open(flag_path, "w").close()
    applied = db.check_and_apply_reset_flag()
    _check(applied is True, "Reset flag détecté et appliqué")
    _check(not os.path.exists(flag_path), "Fichier reset.flag supprimé")
    _check(db.is_first_boot() is True, "first_boot=true après reset")
    auth = main.get_auth()
    _check(auth.count_users() == 0, "0 users après reset")


print()
print("🎉 Tous les tests E2E sprint 10 PASS")

# Cleanup
shutil.rmtree(TEST_DIR, ignore_errors=True)
