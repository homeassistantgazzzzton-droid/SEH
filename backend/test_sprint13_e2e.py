"""
Tests sprint 13 — Perf monitor, endpoints, lazy loading.
"""
import os
import shutil
import sys
import tempfile
import time

# Setup environnement isolé
TEST_DIR = tempfile.mkdtemp(prefix="seh-s13-")
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


# ── Tests perf_monitor en isolé ───────────────────────────────────────
def test_perf_monitor_unit():
    print("\n── Tests unitaires perf_monitor ──")
    import perf_monitor

    # IMPORTANT : le measure_poll utilise le singleton. Donc on utilise le
    # même singleton que celui du module.
    m = perf_monitor.get_monitor()
    m.start()

    info = m.info()
    _check("platform" in info, "info() retourne 'platform'")
    _check("mem_total_mb" in info, "info() retourne 'mem_total_mb'")
    # mem_total_mb : > 0 sur Linux (lit /proc/meminfo), 0 acceptable sur Windows/macOS
    import sys as _sys
    if _sys.platform.startswith("linux"):
        _check(info["mem_total_mb"] > 0, f"mem_total_mb > 0 sur Linux (réel: {info['mem_total_mb']})")
    else:
        _check(info["mem_total_mb"] >= 0, f"mem_total_mb lu (Windows/Mac : 0 attendu, réel: {info['mem_total_mb']})")

    # Ne pas throttle au démarrage
    _check(not m.should_throttle(), "Pas de throttle au démarrage")
    _check(m.get_throttle_factor() == 1.0, "throttle_factor = 1.0 par défaut")

    # measure_poll
    with perf_monitor.measure_poll():
        time.sleep(0.01)
    _check(len(m._poll_latencies) > 0, "measure_poll enregistre la latence")

    time.sleep(6)  # un sample
    snap = m.snapshot()
    _check(snap is not None, "Snapshot disponible après 5s")
    # RSS : > 0 sur Linux (lit /proc/self/statm), 0 acceptable sur non-Linux
    if _sys.platform.startswith("linux"):
        _check(snap.rss_mb > 0, f"RSS > 0 sur Linux (réel: {snap.rss_mb} Mo)")
        _check(snap.rss_mb < 200, f"RSS < 200 Mo (Python idle reste léger)")
    else:
        _check(snap.rss_mb >= 0, f"RSS lu (Windows/Mac : 0 attendu, réel: {snap.rss_mb} Mo)")

    history = m.history(last_n=10)
    _check(len(history) > 0, "history() retourne des samples")


# ── Tests adaptive_sleep ──────────────────────────────────────────────
async def test_adaptive_sleep():
    """Vérifie que adaptive_sleep applique bien le throttle factor."""
    import perf_monitor
    print("\n── Tests adaptive_sleep ──")

    # Pas de throttle → sleep normal
    t0 = time.monotonic()
    await main.adaptive_sleep(0.1)
    elapsed = time.monotonic() - t0
    _check(0.08 < elapsed < 0.20, f"adaptive_sleep(0.1) sans throttle ≈ 0.1s (réel: {elapsed:.3f}s)")

    # Forcer throttle
    m = perf_monitor.get_monitor()
    m._throttled = True
    # Forcer factor = 2.0 (CPU < 90%)
    m._history.clear()  # pas de snapshot avec CPU>=90 → factor=2

    t0 = time.monotonic()
    await main.adaptive_sleep(0.1)
    elapsed = time.monotonic() - t0
    _check(0.18 < elapsed < 0.30, f"adaptive_sleep(0.1) avec throttle≈ 0.2s (réel: {elapsed:.3f}s)")

    m._throttled = False  # reset


# ── Tests endpoints /api/perf ─────────────────────────────────────────
def test_perf_endpoints():
    print("\n── Tests endpoints /api/perf ──")
    with TestClient(main.app) as client:
        # Créer un admin pour passer l'auth
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

        # /api/perf
        r = client.get("/api/perf")
        _check(r.status_code == 200, "GET /api/perf accessible")
        data = r.json()
        _check("info" in data, "/api/perf retourne 'info'")
        _check("current" in data, "/api/perf retourne 'current'")
        _check("throttled" in data, "/api/perf retourne 'throttled'")
        _check(data["throttled"] is False, "throttled=false par défaut")

        # /api/perf/history
        r = client.get("/api/perf/history?minutes=5")
        _check(r.status_code == 200, "GET /api/perf/history accessible")
        data = r.json()
        _check("samples" in data, "/api/perf/history retourne 'samples'")
        _check(isinstance(data["samples"], list), "samples est une liste")


# ── Test lazy-load (modules désactivés ne s'importent pas) ────────────
def test_lazy_load():
    print("\n── Tests lazy-load ──")
    # Au démarrage avec INVERTER_TYPE=none et tout désactivé,
    # les modules MQTT/forecast/influx/solax NE SONT PAS importés.
    # On vérifie que sys.modules n'a pas pollué.
    import sys

    # Liste des modules qui ne doivent PAS être chargés si désactivés
    optional_modules = [
        "mqtt_publisher",
        "solar_forecast",
        "influx_publisher",
        "solax_fleet",
        "alerts",
    ]

    for mod in optional_modules:
        if mod in sys.modules:
            print(f"  ⚠️  {mod} dans sys.modules (normal si activé dans config)")
        else:
            print(f"  ✓ {mod} non chargé (lazy)")

    # Au moins certains doivent ne pas être chargés (cfg vide)
    not_loaded = [m for m in optional_modules if m not in sys.modules]
    _check(len(not_loaded) >= 3,
           f"Au moins 3 modules optionnels non chargés (lazy): {not_loaded}")


# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio
    test_perf_monitor_unit()
    asyncio.run(test_adaptive_sleep())
    test_perf_endpoints()
    test_lazy_load()
    print("\n🎉 Tous les tests sprint 13 PASS")
    shutil.rmtree(TEST_DIR, ignore_errors=True)
