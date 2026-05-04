"""
Smart Energy Hub — Tests unitaires

Couverture :
  - Migrations DB (v1 → v2)
  - Agrégation correcte import/export (vérifie la régression du sprint 4)
  - Backfill export historique
  - Endpoint /api/fleet : stats multi-groupes
  - Endpoint /api/roi : calculs rentabilité
  - Endpoint /api/today_vs_yesterday
  - Endpoint /health
  - Inversion de signe Victron
  - Calcul battery health

Lancer depuis backend/ : pytest tests.py -v
"""
import os
import sys
import time
import tempfile
import sqlite3
import pytest
from pathlib import Path

# Assure que les modules backend sont importables
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import EnergyDatabase
from config_manager import ConfigManager


# ═══════════════════════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tmp_db_path(tmp_path):
    """Chemin temporaire vers une DB de test."""
    return str(tmp_path / "test.db")


@pytest.fixture
def db(tmp_db_path):
    """Instance EnergyDatabase initialisée et prête."""
    d = EnergyDatabase(db_path=tmp_db_path)
    d.init()
    yield d
    d.close()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """ConfigManager sur un fichier temp."""
    cfg_path = str(tmp_path / "config.json")
    monkeypatch.setenv("CONFIG_PATH", cfg_path)
    c = ConfigManager(config_path=cfg_path)
    c.load()
    return c


# ═══════════════════════════════════════════════════════════════════════════
#  Tests DB : migration et schéma
# ═══════════════════════════════════════════════════════════════════════════

class TestMigration:
    def test_fresh_db_has_all_kwh_columns(self, db):
        """Une DB fraîche doit avoir les colonnes kWh dans samples_5min."""
        cols = [r[1] for r in db._conn.execute("PRAGMA table_info(samples_5min)").fetchall()]
        for c in ["pv_kwh", "load_kwh", "import_kwh", "export_kwh",
                  "bat_charge_kwh", "bat_discharge_kwh"]:
            assert c in cols, f"Colonne {c} manquante dans samples_5min"

    def test_migration_v1_to_v2(self, tmp_db_path):
        """Une DB v1 (sans colonnes kWh dans 5min) doit être migrée."""
        # Créer une DB v1 factice
        conn = sqlite3.connect(tmp_db_path)
        conn.executescript("""
            CREATE TABLE samples_raw (ts REAL, pv_power REAL, load_power REAL,
                grid_power REAL, bat_power REAL, bat_soc REAL, bat_voltage REAL,
                frequency REAL, temperature REAL);
            CREATE TABLE samples_5min (
                ts REAL, pv_avg REAL, pv_max REAL, load_avg REAL, load_max REAL,
                grid_avg REAL, grid_min REAL, grid_max REAL, bat_avg REAL,
                bat_min REAL, bat_max REAL, soc_avg REAL, soc_min REAL,
                soc_max REAL, samples INTEGER
            );
            CREATE TABLE samples_hourly (ts REAL, pv_avg REAL, pv_max REAL,
                load_avg REAL, load_max REAL, grid_avg REAL, grid_min REAL,
                grid_max REAL, bat_avg REAL, bat_min REAL, bat_max REAL,
                soc_avg REAL, soc_min REAL, soc_max REAL, pv_kwh REAL,
                load_kwh REAL, import_kwh REAL, export_kwh REAL,
                bat_charge_kwh REAL, bat_discharge_kwh REAL, samples INTEGER);
            CREATE TABLE samples_daily (ts REAL, date_str TEXT UNIQUE,
                pv_avg REAL, pv_max REAL, load_avg REAL, load_max REAL,
                grid_avg REAL, soc_avg REAL, soc_min REAL, soc_max REAL,
                pv_kwh REAL, load_kwh REAL, import_kwh REAL, export_kwh REAL,
                bat_charge_kwh REAL, bat_discharge_kwh REAL,
                self_sufficiency REAL, samples INTEGER);
        """)
        # Insérer une ligne pour vérifier qu'elle survit
        conn.execute("INSERT INTO samples_5min (ts, pv_avg, grid_avg, samples) VALUES (?,?,?,?)",
                     (1000, 500, 100, 30))
        conn.commit()
        conn.close()

        # Lancer la migration
        db = EnergyDatabase(db_path=tmp_db_path)
        db.init()
        cols = [r[1] for r in db._conn.execute("PRAGMA table_info(samples_5min)").fetchall()]
        for c in ["pv_kwh", "load_kwh", "import_kwh", "export_kwh",
                  "bat_charge_kwh", "bat_discharge_kwh"]:
            assert c in cols
        # La ligne existante doit toujours être là
        rows = db._conn.execute("SELECT * FROM samples_5min").fetchall()
        assert len(rows) == 1
        db.close()

    def test_migration_is_idempotent(self, db):
        """Relancer _migrate() sur une DB déjà migrée ne doit pas échouer."""
        db._migrate()
        db._migrate()  # 2e fois, doit être no-op
        cols = [r[1] for r in db._conn.execute("PRAGMA table_info(samples_5min)").fetchall()]
        # Les colonnes ne doivent pas apparaître deux fois
        assert cols.count("import_kwh") == 1


# ═══════════════════════════════════════════════════════════════════════════
#  Tests DB : agrégation import/export (régression sprint 4)
# ═══════════════════════════════════════════════════════════════════════════

class TestAggregation:
    def _inject_raw_samples(self, db, start_ts, duration_s, pv, load, grid, bat, soc=50):
        """Insère des samples toutes les 10s sur la durée donnée."""
        n = duration_s // 10
        for i in range(n):
            db._conn.execute(
                "INSERT INTO samples_raw (ts, pv_power, load_power, grid_power, "
                "bat_power, bat_soc, bat_voltage, frequency, temperature) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (start_ts + i*10, pv, load, grid, bat, soc, 50, 50, 25)
            )

    def test_aggregation_captures_alternating_import_export(self, db):
        """
        Régression sprint 4 : un créneau horaire qui alterne import et export
        doit comptabiliser les deux correctement, pas une moyenne qui annule.
        """
        now = time.time()
        # 30 min d'import à +1500W puis 30 min d'export à -500W
        self._inject_raw_samples(db, now - 3600, 1800, pv=0, load=1500, grid=1500, bat=0)
        self._inject_raw_samples(db, now - 1800, 1800, pv=3000, load=2500, grid=-500, bat=0)
        db._conn.commit()

        # Agréger les 12 slots 5min
        for i in range(12):
            db._aggregate_5min(now - 3600 + (i + 1) * 300)
        db._aggregate_hourly(now)
        db._conn.commit()

        # Vérifier sur l'hourly
        rows = db._conn.execute(
            "SELECT import_kwh, export_kwh, pv_kwh, load_kwh FROM samples_hourly"
        ).fetchall()
        assert len(rows) == 1
        imp, exp, pv_k, ld_k = rows[0]
        # Attendu : 1500W × 0.5h = 0.75 kWh import, 500W × 0.5h = 0.25 kWh export
        # Tolérance 5% pour les effets de bord (sample manquant à la frontière)
        assert 0.70 <= imp <= 0.76, f"Import attendu ~0.75, obtenu {imp}"
        assert 0.23 <= exp <= 0.26, f"Export attendu ~0.25, obtenu {exp}"
        assert 1.45 <= pv_k <= 1.52, f"PV attendu ~1.5, obtenu {pv_k}"
        assert 1.95 <= ld_k <= 2.05, f"Load attendu ~2.0, obtenu {ld_k}"

    def test_aggregation_pure_export(self, db):
        """Une heure 100% export → import_kwh=0 strictement, export_kwh>0."""
        now = time.time()
        self._inject_raw_samples(db, now - 3600, 3600, pv=3000, load=500, grid=-2500, bat=0)
        db._conn.commit()

        for i in range(12):
            db._aggregate_5min(now - 3600 + (i + 1) * 300)
        db._aggregate_hourly(now)

        r = db._conn.execute("SELECT import_kwh, export_kwh FROM samples_hourly").fetchone()
        assert r[0] == 0 or r[0] < 0.01, f"Import devrait être ~0, obtenu {r[0]}"
        assert r[1] > 2.0, f"Export devrait être >2 kWh, obtenu {r[1]}"

    def test_aggregation_pure_import(self, db):
        """Une heure 100% import → export_kwh=0, import_kwh>0."""
        now = time.time()
        self._inject_raw_samples(db, now - 3600, 3600, pv=0, load=1000, grid=1000, bat=0)
        db._conn.commit()

        for i in range(12):
            db._aggregate_5min(now - 3600 + (i + 1) * 300)
        db._aggregate_hourly(now)

        r = db._conn.execute("SELECT import_kwh, export_kwh FROM samples_hourly").fetchone()
        assert r[0] > 0.9, f"Import devrait être ~1 kWh, obtenu {r[0]}"
        assert r[1] == 0 or r[1] < 0.01, f"Export devrait être ~0, obtenu {r[1]}"


# ═══════════════════════════════════════════════════════════════════════════
#  Tests DB : backfill
# ═══════════════════════════════════════════════════════════════════════════

class TestBackfill:
    def test_backfill_only_touches_days_with_zero_export(self, db):
        """Le backfill ne doit PAS modifier les jours avec export_kwh déjà correct."""
        now = time.time()
        # Jour 1 : export déjà correct
        db._conn.execute(
            "INSERT INTO samples_daily (ts, date_str, pv_kwh, load_kwh, import_kwh, "
            "export_kwh, bat_charge_kwh, bat_discharge_kwh) VALUES (?,?,?,?,?,?,?,?)",
            (now - 86400, "2025-01-01", 30.0, 15.0, 2.0, 10.0, 4.0, 3.0)
        )
        # Jour 2 : export=0 mais PV>Load → à backfiller
        db._conn.execute(
            "INSERT INTO samples_daily (ts, date_str, pv_kwh, load_kwh, import_kwh, "
            "export_kwh, bat_charge_kwh, bat_discharge_kwh) VALUES (?,?,?,?,?,?,?,?)",
            (now, "2025-01-02", 30.0, 15.0, 2.0, 0.0, 4.0, 3.0)
        )
        db._conn.commit()

        r = db.backfill_historical_export(dry_run=False)
        assert r["days_fixed"] == 1

        # Jour 1 intact
        d1 = db._conn.execute("SELECT export_kwh FROM samples_daily WHERE date_str='2025-01-01'").fetchone()
        assert d1[0] == 10.0

        # Jour 2 estimé : 30 - 15 + 2 + 3 - 4 = 16
        d2 = db._conn.execute("SELECT export_kwh FROM samples_daily WHERE date_str='2025-01-02'").fetchone()
        assert d2[0] == 16.0

    def test_backfill_idempotent(self, db):
        now = time.time()
        db._conn.execute(
            "INSERT INTO samples_daily (ts, date_str, pv_kwh, load_kwh, import_kwh, "
            "export_kwh, bat_charge_kwh, bat_discharge_kwh) VALUES (?,?,?,?,?,?,?,?)",
            (now, "2025-01-03", 25.0, 15.0, 1.0, 0.0, 2.0, 1.5)
        )
        db._conn.commit()

        r1 = db.backfill_historical_export(dry_run=False)
        assert r1["days_fixed"] == 1
        r2 = db.backfill_historical_export(dry_run=False)
        assert r2["days_fixed"] == 0, "Le 2e passage ne doit rien toucher"

    def test_backfill_dry_run_does_not_modify(self, db):
        now = time.time()
        db._conn.execute(
            "INSERT INTO samples_daily (ts, date_str, pv_kwh, load_kwh, import_kwh, "
            "export_kwh, bat_charge_kwh, bat_discharge_kwh) VALUES (?,?,?,?,?,?,?,?)",
            (now, "2025-01-04", 40.0, 10.0, 0.5, 0.0, 1.0, 0.5)
        )
        db._conn.commit()

        db.backfill_historical_export(dry_run=True)
        # Rien ne doit avoir changé
        d = db._conn.execute("SELECT export_kwh FROM samples_daily WHERE date_str='2025-01-04'").fetchone()
        assert d[0] == 0.0


# ═══════════════════════════════════════════════════════════════════════════
#  Tests config
# ═══════════════════════════════════════════════════════════════════════════

class TestConfig:
    def test_default_config_has_roi_section(self, cfg):
        c = cfg.get()
        assert "roi" in c
        assert c["roi"]["installation_cost"] == 0

    def test_default_config_has_invert_signs(self, cfg):
        c = cfg.get()
        assert "invert_grid_sign" in c["victron"]
        assert "invert_battery_sign" in c["victron"]
        assert c["victron"]["invert_grid_sign"] is False

    def test_default_config_has_enriched_alerts(self, cfg):
        c = cfg.get()
        a = c["alerts"]
        for k in ["pv_low_alert", "pv_low_threshold_pct", "source_stale_alert",
                  "source_stale_minutes", "cycles_alert", "cycles_threshold",
                  "weekly_summary", "weekly_summary_hour"]:
            assert k in a, f"Clé {k} manquante dans alerts"


# ═══════════════════════════════════════════════════════════════════════════
#  Tests API (avec TestClient FastAPI)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def api_client(tmp_path, monkeypatch):
    """TestClient FastAPI avec DB + config temp."""
    cfg_path = str(tmp_path / "config.json")
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("CONFIG_PATH", cfg_path)
    monkeypatch.setenv("DB_PATH", db_path)

    # Le mount /app/frontend doit exister
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html></html>")
    monkeypatch.chdir(tmp_path)

    # Monkey patch le mount statique pour utiliser notre temp dir
    import main
    # Forcer re-init des globaux
    main._cfg = ConfigManager(config_path=cfg_path)
    main._cfg.load()
    main._db = EnergyDatabase(db_path=db_path)
    main._db.init()

    # Patcher le path du frontend pour pas crasher
    from fastapi.staticfiles import StaticFiles
    from fastapi.testclient import TestClient
    # Retirer le mount "/" s'il existe
    main.app.router.routes = [
        r for r in main.app.router.routes
        if not (hasattr(r, "path") and r.path == "/")
    ]
    main.app.mount("/static_test", StaticFiles(directory=str(frontend_dir), html=True))

    return TestClient(main.app), main


class TestFleetAPI:
    def test_empty_fleet(self, api_client):
        client, main = api_client
        main._cache["bms_groups"] = {}
        r = client.get("/api/fleet")
        assert r.status_code == 200
        d = r.json()
        assert d["stats"]["total_count"] == 0

    def test_fleet_with_mixed_groups(self, api_client):
        client, main = api_client
        main._cache["bms_groups"] = {
            "jkbms_0": {
                "name": "JK-BMS", "type": "jkbms",
                "units": {
                    "1": {"online": True, "soc": 85, "soh": 99, "voltage": 51.2,
                          "current": 10, "power": 512, "temperature": 25,
                          "nominal_capacity": 280, "remaining_capacity": 238,
                          "cycle_count": 45, "base_state": "Charge",
                          "alarm_count": 0, "alarms": [],
                          "voltage_state": "Normal", "current_state": "Normal",
                          "temperature_state": "Normal"},
                    "2": {"online": False},
                }
            },
            "pylontech_0": {
                "name": "Pylontech", "type": "pylontech",
                "units": {
                    "1": {"online": True, "soc": 70, "voltage": 50.0,
                          "current": 5.0, "power": 250, "temperature": 26,
                          "cycle_count": 130, "base_state": "Charge",
                          "alarm_count": 0, "alarms": []},
                }
            }
        }
        r = client.get("/api/fleet")
        assert r.status_code == 200
        d = r.json()
        assert d["stats"]["online"] == 2
        assert d["stats"]["offline"] == 1
        assert d["stats"]["total_count"] == 3
        # SoC moyen = (85 + 70) / 2 = 77.5
        assert d["stats"]["avg_soc"] == 77.5


class TestROIAPI:
    def test_empty_config_returns_not_enabled(self, api_client):
        client, _ = api_client
        r = client.get("/api/roi")
        assert r.status_code == 200
        d = r.json()
        assert d["config"]["enabled"] is False

    def test_roi_with_data(self, api_client):
        client, main = api_client
        # Config
        cfg = main._cfg.get()
        cfg["roi"]["installation_cost"] = 10000
        cfg["finance"]["import_price"] = 0.25
        cfg["finance"]["export_price"] = 0.13
        main._cfg.update(cfg)

        # 100 jours de data
        now = time.time()
        for i in range(100):
            ts = now - i * 86400
            import datetime
            date_str = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            main._db._conn.execute(
                "INSERT INTO samples_daily (ts, date_str, pv_kwh, load_kwh, "
                "import_kwh, export_kwh) VALUES (?,?,?,?,?,?)",
                (ts, date_str, 30, 20, 5, 8)
            )
        main._db._conn.commit()

        r = client.get("/api/roi")
        assert r.status_code == 200
        d = r.json()
        assert d["summary"]["days_of_data"] == 100
        assert d["summary"]["total_savings"] > 0
        assert d["summary"]["total_export_kwh"] > 0


class TestTodayVsYesterday:
    def test_empty(self, api_client):
        client, _ = api_client
        r = client.get("/api/today_vs_yesterday")
        assert r.status_code == 200
        d = r.json()
        assert d["today"].get("empty") is True
        assert d["yesterday"].get("empty") is True

    def test_with_data(self, api_client):
        client, main = api_client
        import datetime
        now = datetime.datetime.now()
        for day_offset, pv in [(0, 25.0), (1, 20.0)]:
            ts = (now - datetime.timedelta(days=day_offset)).timestamp()
            date_str = (now - datetime.timedelta(days=day_offset)).strftime("%Y-%m-%d")
            main._db._conn.execute(
                "INSERT INTO samples_daily (ts, date_str, pv_kwh, load_kwh, "
                "import_kwh, export_kwh) VALUES (?,?,?,?,?,?)",
                (ts, date_str, pv, 15, 2, 5)
            )
        main._db._conn.commit()

        r = client.get("/api/today_vs_yesterday")
        d = r.json()
        assert d["today"]["pv_kwh"] == 25.0
        assert d["yesterday"]["pv_kwh"] == 20.0
        # Delta +5 kWh, +25%
        assert d["deltas"]["pv_kwh"]["diff"] == 5.0
        assert d["deltas"]["pv_kwh"]["pct"] == 25.0

    def test_includes_month_and_year(self, api_client):
        """Le widget dashboard a besoin des totaux mois et année."""
        client, main = api_client
        import datetime
        now = datetime.datetime.now()
        # 5 jours du mois courant
        for i in range(5):
            d = now - datetime.timedelta(days=i)
            main._db._conn.execute(
                "INSERT INTO samples_daily (ts, date_str, pv_kwh, load_kwh, "
                "import_kwh, export_kwh) VALUES (?,?,?,?,?,?)",
                (d.timestamp(), d.strftime("%Y-%m-%d"), 30.0, 20, 5, 8)
            )
        main._db._conn.commit()

        r = client.get("/api/today_vs_yesterday")
        d = r.json()
        assert "month" in d
        assert "year" in d
        # 5 jours × 30 kWh = 150 kWh ce mois (au minimum, si tous dans le même mois)
        assert d["month"]["pv_kwh"] >= 30  # au moins 1 jour
        assert d["year"]["pv_kwh"] >= d["month"]["pv_kwh"]
        # Les autres champs doivent être présents
        for k in ["pv_kwh", "load_kwh", "import_kwh", "export_kwh"]:
            assert k in d["month"]
            assert k in d["year"]


class TestSolarForecastLossFactor:
    """Sprint 7 patch : facteur de correction des pertes."""

    def test_default_loss_is_zero(self):
        from solar_forecast import SolarForecast
        sf = SolarForecast({"enabled": True, "latitude": 43.32, "longitude": -0.37,
                            "planes": [{"kwp": 5, "declination": 30, "azimuth": 0}]})
        # Pas de loss_percent défini → defaults à 0 → loss_factor = 1.0
        assert sf._config.get("loss_percent", 0) == 0

    def test_loss_factor_clamping(self):
        """Vérifie que le facteur est borné [0, 1] même avec valeurs extrêmes."""
        # Test direct du calcul interne
        for loss_pct, expected_factor in [
            (0, 1.0), (10, 0.9), (25, 0.75), (50, 0.5),
            (-10, 1.0),  # négatif → 0% perte
            (150, 0.0),  # > 100% → 0
        ]:
            factor = max(0.0, min(1.0, 1.0 - loss_pct / 100.0))
            assert abs(factor - expected_factor) < 0.001, \
                f"loss={loss_pct}% expected factor={expected_factor}, got {factor}"

    def test_update_config_invalidates_cache(self):
        """Changer loss_percent doit invalider le cache."""
        from solar_forecast import SolarForecast
        sf = SolarForecast({"enabled": True, "loss_percent": 0})
        sf._cache = {"hourly": [], "daily": []}
        sf._last_fetch = 999999999
        sf.update_config({"enabled": True, "loss_percent": 20})
        assert sf._cache is None
        assert sf._last_fetch == 0


class TestHealth:
    def test_health_with_db_ok(self, api_client):
        client, _ = api_client
        r = client.get("/health")
        assert r.status_code == 200
        d = r.json()
        assert d["status"] in ("ok", "degraded")
        assert d["checks"]["db"] == "ok"
        assert d["checks"]["config"].startswith("ok")

    def test_health_without_db(self, api_client):
        client, main = api_client
        main._db = None
        r = client.get("/health")
        # Sans DB, on renvoie 503
        assert r.status_code == 503


class TestBatteryHealth:
    def test_empty(self, api_client):
        client, main = api_client
        main._cache["bms_groups"] = {}
        r = client.get("/api/battery_health")
        assert r.status_code == 200
        d = r.json()
        assert d["summary"]["count"] == 0

    def test_soh_estimation_for_pylontech(self, api_client):
        """Pylontech n'expose pas de SoH → il doit être estimé depuis cycles."""
        client, main = api_client
        main._cache["bms_groups"] = {
            "p": {"name": "Pylontech", "type": "pylontech", "units": {
                "1": {"online": True, "cycle_count": 2250, "voltage": 50.0}
            }}
        }
        r = client.get("/api/battery_health")
        d = r.json()
        bat = d["batteries"][0]
        assert bat["soh_reported"] is None
        assert bat["soh_estimated"] is not None
        # 2250 / 4500 cycles × 20% = 10% de dégradation → 90% SoH
        assert 89 <= bat["soh_estimated"] <= 91

    def test_health_status_grades(self, api_client):
        client, main = api_client
        main._cache["bms_groups"] = {
            "j": {"name": "JK", "type": "jkbms", "units": {
                "1": {"online": True, "cycle_count": 100, "soh": 99, "voltage": 51.2},
                "2": {"online": True, "cycle_count": 3000, "soh": 82, "voltage": 51.0},
            }}
        }
        r = client.get("/api/battery_health")
        bats = r.json()["batteries"]
        bat1 = next(b for b in bats if b["id"] == "1")
        bat2 = next(b for b in bats if b["id"] == "2")
        assert bat1["health_status"] == "excellent"
        assert bat2["health_status"] == "warning"


# ═══════════════════════════════════════════════════════════════════════════
#  Tests Sprint 6 — Forecast snapshots, Leaf, Prometheus
# ═══════════════════════════════════════════════════════════════════════════

class TestForecastSnapshots:
    def test_save_and_retrieve(self, db):
        """Sauvegarde de prévisions puis comparaison avec réel."""
        # Injecter des snapshots (prévisions faites hier pour aujourd'hui)
        import datetime as dt
        today = dt.datetime.now().strftime("%Y-%m-%d")
        yesterday = (dt.datetime.now() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        two_days_ago = (dt.datetime.now() - dt.timedelta(days=2)).strftime("%Y-%m-%d")

        db.save_forecast_snapshot([
            {"date": today, "kwh": 25.0},
            {"date": yesterday, "kwh": 22.0},
        ])
        # Injecter une mesure réelle pour yesterday
        db._conn.execute(
            "INSERT INTO samples_daily (ts, date_str, pv_kwh) VALUES (?, ?, ?)",
            (time.time() - 86400, yesterday, 20.0)
        )
        db._conn.commit()

        series = db.get_forecast_vs_actual(days=30)
        # On doit avoir une ligne pour yesterday (today n'a pas encore de réel)
        assert len(series) == 1
        assert series[0]["forecast_kwh"] == 22.0
        assert series[0]["actual_kwh"] == 20.0
        assert series[0]["error_kwh"] == -2.0  # sous-production réelle vs prévu

    def test_snapshot_idempotent(self, db):
        """Re-sauvegarder le même jour doit écraser, pas dupliquer."""
        db.save_forecast_snapshot([{"date": "2025-05-10", "kwh": 25.0}])
        db.save_forecast_snapshot([{"date": "2025-05-10", "kwh": 27.0}])
        rows = db._conn.execute(
            "SELECT COUNT(*), MAX(forecast_kwh) FROM solar_forecast_snapshots "
            "WHERE forecast_date='2025-05-10'"
        ).fetchone()
        assert rows[0] == 1  # une seule ligne après 2 inserts même jour
        assert rows[1] == 27.0  # valeur mise à jour

    def test_accuracy_empty(self, api_client):
        client, _ = api_client
        r = client.get("/api/forecast/accuracy")
        assert r.status_code == 200
        d = r.json()
        assert d["series"] == []
        assert d.get("message")

    def test_accuracy_with_data(self, api_client):
        client, main = api_client
        import datetime as dt
        # 5 jours de data
        for i in range(1, 6):
            d = (dt.datetime.now() - dt.timedelta(days=i)).strftime("%Y-%m-%d")
            main._db.save_forecast_snapshot([{"date": d, "kwh": 20.0 + i}])
            main._db._conn.execute(
                "INSERT INTO samples_daily (ts, date_str, pv_kwh) VALUES (?, ?, ?)",
                (time.time() - i * 86400, d, 18.0 + i)
            )
        main._db._conn.commit()

        r = client.get("/api/forecast/accuracy")
        d = r.json()
        assert d["metrics"]["days_compared"] == 5
        assert d["metrics"]["mae_kwh"] == 2.0
        assert d["metrics"]["quality"] in ("excellent", "good", "fair", "poor")


class TestLeafOptimize:
    def test_disabled_by_default(self, api_client):
        client, _ = api_client
        r = client.get("/api/leaf/optimize")
        assert r.status_code == 200
        d = r.json()
        assert d["enabled"] is False

    def test_needs_charge(self, api_client):
        client, main = api_client
        cfg = main._cfg.get()
        cfg["leaf"]["enabled"] = True
        cfg["leaf"]["current_soc_percent"] = 30
        cfg["leaf"]["target_soc_percent"] = 80
        cfg["leaf"]["battery_capacity_kwh"] = 24
        # Pas de forecast activé → on doit recevoir un message d'erreur clair
        main._cfg.update(cfg)
        r = client.get("/api/leaf/optimize")
        d = r.json()
        assert d["enabled"] is True
        # Sans forecast activé, on a une erreur
        assert "error" in d or d.get("needed_kwh", 0) > 0

    def test_already_charged(self, api_client):
        client, main = api_client
        cfg = main._cfg.get()
        cfg["leaf"]["enabled"] = True
        cfg["leaf"]["current_soc_percent"] = 79.5
        cfg["leaf"]["target_soc_percent"] = 80
        main._cfg.update(cfg)
        r = client.get("/api/leaf/optimize")
        d = r.json()
        # needed_kwh < 0.5 kWh → pas besoin de charger
        assert "message" in d


class TestPrometheus:
    def test_metrics_basic(self, api_client):
        client, _ = api_client
        r = client.get("/metrics")
        assert r.status_code == 200
        body = r.text
        assert "seh_uptime_seconds" in body
        assert body.startswith("# Smart Energy Hub metrics")

    def test_metrics_includes_victron_data(self, api_client):
        client, main = api_client
        main._cache["victron_system"]["data"] = {
            "total_pv_power": 2500, "consumption_power": 800,
            "grid_power": -1200, "battery_power": 500, "battery_soc": 85,
        }
        r = client.get("/metrics")
        body = r.text
        assert "seh_pv_power_watts 2500" in body
        assert "seh_load_power_watts 800" in body
        assert "seh_grid_power_watts -1200" in body
        assert "seh_battery_soc_percent 85" in body

    def test_metrics_includes_bms(self, api_client):
        client, main = api_client
        main._cache["bms_groups"] = {
            "j": {"name": "JK", "type": "jkbms", "units": {
                "1": {"online": True, "soc": 85, "voltage": 51.2, "current": 10, "power": 512}
            }}
        }
        r = client.get("/metrics")
        body = r.text
        assert 'seh_bms_soc_percent{group="JK",type="jkbms",bat="1"} 85' in body
        assert 'seh_bms_power_watts{group="JK",type="jkbms",bat="1"} 512' in body

    def test_metrics_can_be_disabled(self, api_client):
        client, main = api_client
        cfg = main._cfg.get()
        cfg["integrations"]["prometheus_enabled"] = False
        main._cfg.update(cfg)
        r = client.get("/metrics")
        assert r.status_code == 404


class TestInfluxPublisher:
    def test_disabled_by_default(self):
        from influx_publisher import InfluxPublisher
        p = InfluxPublisher({})
        assert not p.enabled

    def test_enabled_requires_url_and_token(self):
        from influx_publisher import InfluxPublisher
        p = InfluxPublisher({"influxdb_enabled": True})
        assert not p.enabled  # pas d'URL, pas de token
        p = InfluxPublisher({
            "influxdb_enabled": True,
            "influxdb_url": "http://localhost:8086",
            "influxdb_token": "xxx",
        })
        assert p.enabled

    def test_format_lines_basic(self):
        from influx_publisher import InfluxPublisher
        p = InfluxPublisher({})
        cache = {
            "victron_system": {"data": {
                "total_pv_power": 2000, "consumption_power": 800,
                "grid_power": -1200, "battery_power": 0, "battery_soc": 85,
            }},
            "solarchargers": {"units": {}},
            "inverter": {"units": {}},
            "bms_groups": {},
        }
        lines = p._format_lines(cache)
        assert len(lines) >= 1
        assert any("seh_system" in l for l in lines)
        # Vérifier le format line-protocol : measurement fields timestamp
        sys_line = next(l for l in lines if l.startswith("seh_system"))
        parts = sys_line.split(" ")
        assert len(parts) == 3  # measurement, fields, ts
        assert "pv_power=2000" in parts[1] or "pv_power=2000.0" in parts[1]

    def test_format_lines_with_bms(self):
        from influx_publisher import InfluxPublisher
        p = InfluxPublisher({})
        cache = {
            "victron_system": {"data": None},
            "solarchargers": {"units": {}},
            "inverter": {"units": {}},
            "bms_groups": {
                "j0": {"name": "JK_DIY", "type": "jkbms", "units": {
                    "1": {"online": True, "soc": 85, "voltage": 51.2,
                          "current": 10, "cycle_count": 45}
                }}
            },
        }
        lines = p._format_lines(cache)
        assert any("seh_bms" in l and "group=JK_DIY" in l for l in lines)


class TestConfig6:
    def test_default_config_has_leaf(self, cfg):
        c = cfg.get()
        assert "leaf" in c
        assert c["leaf"]["battery_capacity_kwh"] == 24

    def test_default_config_has_integrations(self, cfg):
        c = cfg.get()
        assert "integrations" in c
        assert c["integrations"]["prometheus_enabled"] is True
        assert c["integrations"]["influxdb_enabled"] is False


# ═══════════════════════════════════════════════════════════════════════════
#  Tests Sprint 7 patch — Bug doublons d'agrégation
# ═══════════════════════════════════════════════════════════════════════════

class TestUniqueIndexAggregates:
    """Sprint 7 : assure qu'on ne peut plus avoir de doublons sur ts."""

    def test_unique_index_exists_on_fresh_db(self, db):
        """Une DB neuve doit avoir l'UNIQUE INDEX sur ts pour 5min et hourly."""
        for table in ("samples_5min", "samples_hourly"):
            indexes = db._conn.execute(
                f"PRAGMA index_list({table})"
            ).fetchall()
            unique_idx = [i for i in indexes if i[2] == 1]  # i[2] = unique flag
            assert len(unique_idx) >= 1, f"Pas d'UNIQUE INDEX sur {table}"

    def test_aggregate_5min_is_idempotent(self, db):
        """Appeler _aggregate_5min plusieurs fois pour le même ts ne doit créer qu'une ligne."""
        import time
        now = time.time()
        # Insère 30 samples raw
        for i in range(30):
            db._conn.execute(
                "INSERT INTO samples_raw (ts, pv_power, load_power, grid_power, "
                "bat_power, bat_soc) VALUES (?,?,?,?,?,?)",
                (now - 300 + i*10, 1000, 500, 0, 500, 50)
            )
        db._conn.commit()
        # Appelle 3 fois
        db._aggregate_5min(now)
        db._aggregate_5min(now)
        db._aggregate_5min(now)
        db._conn.commit()
        # Doit avoir UNE seule ligne
        n = db._conn.execute(
            "SELECT COUNT(*) FROM samples_5min WHERE ts = ?", (now,)
        ).fetchone()[0]
        assert n == 1

    def test_aggregate_hourly_is_idempotent(self, db):
        """Idem pour _aggregate_hourly."""
        import time
        now = time.time()
        # Pré-remplit samples_5min
        for i in range(12):
            db._aggregate_5min(now - 3600 + (i+1)*300)
        # Pas de samples raw → l'agrégation 5min ne fait rien
        # On insère directement dans samples_5min
        for i in range(12):
            db._conn.execute(
                "INSERT OR REPLACE INTO samples_5min (ts, pv_avg, pv_kwh, samples) "
                "VALUES (?, ?, ?, ?)", (now - 3000 + i*300, 1000, 0.083, 30)
            )
        db._conn.commit()
        # Trois appels
        db._aggregate_hourly(now)
        db._aggregate_hourly(now)
        db._aggregate_hourly(now)
        n = db._conn.execute(
            "SELECT COUNT(*) FROM samples_hourly WHERE ts = ?", (now,)
        ).fetchone()[0]
        assert n == 1


class TestMigrationV3:
    """Sprint 7 : migration v3 dédoublonne les anciennes DB et crée UNIQUE INDEX."""

    def test_v3_dedup_on_legacy_db(self, tmp_path):
        """Une DB legacy avec doublons doit être nettoyée au premier init."""
        import sqlite3
        db_path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE samples_raw (ts REAL, pv_power REAL, load_power REAL,
                grid_power REAL, bat_power REAL, bat_soc REAL, bat_voltage REAL,
                frequency REAL, temperature REAL);
            CREATE TABLE samples_5min (
                ts REAL, pv_avg REAL, pv_max REAL, load_avg REAL, load_max REAL,
                grid_avg REAL, grid_min REAL, grid_max REAL, bat_avg REAL,
                bat_min REAL, bat_max REAL, soc_avg REAL, soc_min REAL, soc_max REAL,
                pv_kwh REAL DEFAULT 0, load_kwh REAL DEFAULT 0,
                import_kwh REAL DEFAULT 0, export_kwh REAL DEFAULT 0,
                bat_charge_kwh REAL DEFAULT 0, bat_discharge_kwh REAL DEFAULT 0,
                samples INTEGER
            );
            CREATE TABLE samples_hourly (
                ts REAL, pv_avg REAL, pv_max REAL, load_avg REAL, load_max REAL,
                grid_avg REAL, grid_min REAL, grid_max REAL, bat_avg REAL,
                bat_min REAL, bat_max REAL, soc_avg REAL, soc_min REAL, soc_max REAL,
                pv_kwh REAL, load_kwh REAL, import_kwh REAL, export_kwh REAL,
                bat_charge_kwh REAL, bat_discharge_kwh REAL, samples INTEGER
            );
            CREATE TABLE samples_daily (ts REAL, date_str TEXT UNIQUE,
                pv_avg REAL, pv_max REAL, load_avg REAL, load_max REAL,
                grid_avg REAL, soc_avg REAL, soc_min REAL, soc_max REAL,
                pv_kwh REAL, load_kwh REAL, import_kwh REAL, export_kwh REAL,
                bat_charge_kwh REAL, bat_discharge_kwh REAL,
                self_sufficiency REAL, samples INTEGER);
        """)
        # Injecte 24h de samples_hourly avec 3 doublons par ts
        import time
        now = time.time()
        for h in range(24):
            ts = now - 86400 + h*3600
            for dup in range(3):
                conn.execute(
                    "INSERT INTO samples_hourly (ts, pv_kwh, load_kwh, samples) "
                    "VALUES (?, ?, ?, ?)", (ts, 1.5, 0.8, 360)
                )
        conn.commit()
        # 72 lignes au départ
        assert conn.execute("SELECT COUNT(*) FROM samples_hourly").fetchone()[0] == 72
        conn.close()

        # Init la DB → migration auto
        from database import EnergyDatabase
        db = EnergyDatabase(db_path=db_path)
        db.init()
        # Devrait avoir réduit à 24
        n = db._conn.execute("SELECT COUNT(*) FROM samples_hourly").fetchone()[0]
        assert n == 24
        # UNIQUE INDEX doit être présent
        idx = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE name='idx_hourly_ts_unique'"
        ).fetchone()
        assert idx is not None
        db.close()

    def test_v3_recompute_daily_after_dedup(self, tmp_path):
        """Après dédoublonnage, samples_daily doit être recomputé avec les bonnes valeurs."""
        import sqlite3
        db_path = str(tmp_path / "legacy2.db")
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE samples_raw (ts REAL, pv_power REAL, load_power REAL,
                grid_power REAL, bat_power REAL, bat_soc REAL, bat_voltage REAL,
                frequency REAL, temperature REAL);
            CREATE TABLE samples_5min (ts REAL, pv_avg REAL, pv_max REAL,
                load_avg REAL, load_max REAL, grid_avg REAL, grid_min REAL,
                grid_max REAL, bat_avg REAL, bat_min REAL, bat_max REAL,
                soc_avg REAL, soc_min REAL, soc_max REAL,
                pv_kwh REAL DEFAULT 0, load_kwh REAL DEFAULT 0,
                import_kwh REAL DEFAULT 0, export_kwh REAL DEFAULT 0,
                bat_charge_kwh REAL DEFAULT 0, bat_discharge_kwh REAL DEFAULT 0,
                samples INTEGER);
            CREATE TABLE samples_hourly (ts REAL, pv_avg REAL, pv_max REAL,
                load_avg REAL, load_max REAL, grid_avg REAL, grid_min REAL,
                grid_max REAL, bat_avg REAL, bat_min REAL, bat_max REAL,
                soc_avg REAL, soc_min REAL, soc_max REAL,
                pv_kwh REAL, load_kwh REAL, import_kwh REAL, export_kwh REAL,
                bat_charge_kwh REAL, bat_discharge_kwh REAL, samples INTEGER);
            CREATE TABLE samples_daily (ts REAL, date_str TEXT UNIQUE,
                pv_avg REAL, pv_max REAL, load_avg REAL, load_max REAL,
                grid_avg REAL, soc_avg REAL, soc_min REAL, soc_max REAL,
                pv_kwh REAL, load_kwh REAL, import_kwh REAL, export_kwh REAL,
                bat_charge_kwh REAL, bat_discharge_kwh REAL,
                self_sufficiency REAL, samples INTEGER);
        """)
        # Insère 24 heures (avec doublons ×2) sur une journée précise (hier)
        import datetime as dt
        yday = dt.datetime.now() - dt.timedelta(days=1)
        for h in range(24):
            ts = yday.replace(hour=h, minute=0, second=0, microsecond=0).timestamp()
            for dup in range(2):
                conn.execute(
                    "INSERT INTO samples_hourly (ts, pv_kwh, load_kwh, samples) "
                    "VALUES (?, ?, ?, ?)", (ts, 1.0, 0.5, 360)
                )
        # daily faux : sum doublé
        conn.execute(
            "INSERT INTO samples_daily (ts, date_str, pv_kwh, load_kwh, samples) "
            "VALUES (?, ?, ?, ?, ?)",
            (yday.timestamp(), yday.strftime("%Y-%m-%d"), 48.0, 24.0, 17280)
        )
        conn.commit()
        conn.close()

        from database import EnergyDatabase
        db = EnergyDatabase(db_path=db_path)
        db.init()  # déclenche migration

        # Daily recomputé : 24h × 1.0 = 24 (pas 48)
        d = db._conn.execute(
            "SELECT pv_kwh, load_kwh FROM samples_daily WHERE date_str=?",
            (yday.strftime("%Y-%m-%d"),)
        ).fetchone()
        assert d is not None, "samples_daily devrait exister"
        assert 23.5 <= d[0] <= 24.5, f"pv_kwh attendu ~24, obtenu {d[0]}"
        assert 11.5 <= d[1] <= 12.5, f"load_kwh attendu ~12, obtenu {d[1]}"
        db.close()

    def test_v3_idempotent(self, tmp_path):
        """Relancer init sur une DB déjà migrée ne doit pas re-déclencher la migration."""
        from database import EnergyDatabase
        db_path = str(tmp_path / "fresh.db")
        db1 = EnergyDatabase(db_path=db_path)
        db1.init()
        # Vérifier que le marqueur n'a pas été créé (pas de migration nécessaire pour DB fresh)
        # (CREATE INDEX a réussi mais sans dedupe car pas de doublons)
        marker = db1._conn.execute(
            "SELECT name FROM sqlite_master WHERE name='_seh_v3_daily_recomputed'"
        ).fetchone()
        # Le marqueur n'est créé que si dédoublonnage effectif → sur DB neuve, pas de marqueur
        # ou bien il est créé mais avec 0 daily → c'est OK
        db1.close()

        # 2e init : ne doit rien faire de bizarre
        db2 = EnergyDatabase(db_path=db_path)
        db2.init()  # Pas d'exception
        # Vérifier que UNIQUE INDEX existe toujours
        idx = db2._conn.execute(
            "SELECT name FROM sqlite_master WHERE name='idx_hourly_ts_unique'"
        ).fetchone()
        assert idx is not None
        db2.close()


class TestDedupeAggregates:
    """Endpoint dedupe_aggregates manuel."""

    def test_dry_run_does_not_modify(self, db):
        import time
        now = time.time()
        # Insère via INSERT direct (pas _aggregate_*) pour bypass UNIQUE
        # (pas possible avec UNIQUE INDEX en place → on test sur DB fresh sans doublons)
        db._conn.execute(
            "INSERT INTO samples_hourly (ts, pv_kwh, samples) VALUES (?, ?, ?)",
            (now, 1.5, 360)
        )
        db._conn.commit()
        before = db._conn.execute("SELECT COUNT(*) FROM samples_hourly").fetchone()[0]
        r = db.dedupe_aggregates(dry_run=True)
        after = db._conn.execute("SELECT COUNT(*) FROM samples_hourly").fetchone()[0]
        assert before == after
        assert r["status"] == "ok"
        assert r["dry_run"] is True

    def test_dedupe_no_duplicates_returns_zero(self, db):
        """Sur une DB sans doublons, dedupe doit rapporter 0."""
        r = db.dedupe_aggregates(dry_run=False)
        assert r["status"] == "ok"
        assert r["5min"]["duplicate_ts_groups"] == 0
        assert r["hourly"]["duplicate_ts_groups"] == 0


class TestMigrationV4KwhBackfill:
    """Sprint 7+ : migration v4 recalcule les colonnes kWh historiques manquantes."""

    def test_v4_backfills_zero_kwh_with_avg(self, tmp_path):
        """Une 5min avec pv_avg>0 mais pv_kwh=0 doit être recalculée."""
        import sqlite3
        db_path = str(tmp_path / "v4.db")
        # DB minimale avec 5min vides en kWh
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE samples_raw (ts REAL, pv_power REAL, load_power REAL,
                grid_power REAL, bat_power REAL, bat_soc REAL, bat_voltage REAL,
                frequency REAL, temperature REAL);
            CREATE TABLE samples_5min (
                ts REAL, pv_avg REAL, pv_max REAL, load_avg REAL, load_max REAL,
                grid_avg REAL, grid_min REAL, grid_max REAL, bat_avg REAL,
                bat_min REAL, bat_max REAL, soc_avg REAL, soc_min REAL, soc_max REAL,
                pv_kwh REAL DEFAULT 0, load_kwh REAL DEFAULT 0,
                import_kwh REAL DEFAULT 0, export_kwh REAL DEFAULT 0,
                bat_charge_kwh REAL DEFAULT 0, bat_discharge_kwh REAL DEFAULT 0,
                samples INTEGER
            );
            CREATE TABLE samples_hourly (
                ts REAL, pv_avg REAL, pv_max REAL, load_avg REAL, load_max REAL,
                grid_avg REAL, grid_min REAL, grid_max REAL, bat_avg REAL,
                bat_min REAL, bat_max REAL, soc_avg REAL, soc_min REAL, soc_max REAL,
                pv_kwh REAL, load_kwh REAL, import_kwh REAL, export_kwh REAL,
                bat_charge_kwh REAL, bat_discharge_kwh REAL, samples INTEGER
            );
            CREATE TABLE samples_daily (ts REAL, date_str TEXT UNIQUE,
                pv_avg REAL, pv_max REAL, load_avg REAL, load_max REAL,
                grid_avg REAL, soc_avg REAL, soc_min REAL, soc_max REAL,
                pv_kwh REAL, load_kwh REAL, import_kwh REAL, export_kwh REAL,
                bat_charge_kwh REAL, bat_discharge_kwh REAL,
                self_sufficiency REAL, samples INTEGER);
        """)
        # Injecte 12 slots 5min avec puissances moyennes mais kWh=0
        # 1 heure pleine : pv_avg=1500W, load_avg=500W, grid_avg=-1000W (export)
        import time
        now = time.time()
        for i in range(12):
            ts = now - 3600 + (i+1)*300
            conn.execute(
                "INSERT INTO samples_5min (ts, pv_avg, load_avg, grid_avg, bat_avg, "
                "soc_avg, samples, pv_kwh, load_kwh) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)",
                (ts, 1500, 500, -1000, 0, 80, 30)
            )
        conn.commit()
        conn.close()

        # Init la DB → migration v4 doit recalculer
        from database import EnergyDatabase
        db = EnergyDatabase(db_path=db_path)
        db.init()

        # Vérifier que pv_kwh a été calculé : 1500W × 5min/60min/1000 = 0.125 kWh
        rows = db._conn.execute(
            "SELECT pv_kwh, load_kwh, export_kwh FROM samples_5min ORDER BY ts"
        ).fetchall()
        assert len(rows) == 12
        for pv_kwh, load_kwh, exp_kwh in rows:
            assert 0.12 < pv_kwh < 0.13, f"pv_kwh attendu ~0.125, obtenu {pv_kwh}"
            assert 0.04 < load_kwh < 0.05, f"load_kwh attendu ~0.0417, obtenu {load_kwh}"
            assert 0.08 < exp_kwh < 0.09, f"export_kwh attendu ~0.0833, obtenu {exp_kwh}"

        # Hourly doit avoir été reconstruit depuis 5min recalculés
        # NB : selon le moment où le test tourne (avant ou après l'heure pleine),
        # il peut n'y avoir aucun bloc d'heure complet → on tolère 0 ou 1+
        hourly = db._conn.execute(
            "SELECT pv_kwh, load_kwh FROM samples_hourly"
        ).fetchall()
        # Si un hourly a pu être créé, vérifier ses valeurs
        if len(hourly) >= 1:
            total_pv = sum(h[0] for h in hourly)
            # Au mieux 12 slots de 0.125 = 1.5 kWh. Selon timing du test, 
            # certains slots peuvent être hors fenêtre alignée → on tolère large.
            assert 0.1 < total_pv < 1.6, f"Total hourly pv_kwh attendu 0.1-1.5, obtenu {total_pv}"
        # Sinon : la migration a juste recalculé les 5min (suffit pour le test)

        db.close()

    def test_v4_idempotent(self, tmp_path):
        """Relancer init après migration v4 ne doit pas re-déclencher."""
        from database import EnergyDatabase
        db_path = str(tmp_path / "v4_idem.db")
        db1 = EnergyDatabase(db_path=db_path)
        db1.init()
        # Vérifier marqueur posé
        marker1 = db1._conn.execute(
            "SELECT name FROM sqlite_master WHERE name='_seh_v4_kwh_backfilled'"
        ).fetchone()
        assert marker1 is not None
        db1.close()

        # 2e init : ne doit pas crasher
        db2 = EnergyDatabase(db_path=db_path)
        db2.init()
        marker2 = db2._conn.execute(
            "SELECT name FROM sqlite_master WHERE name='_seh_v4_kwh_backfilled'"
        ).fetchone()
        assert marker2 is not None
        db2.close()

    def test_v4_does_not_touch_already_correct_kwh(self, db):
        """Si un row a pv_kwh > 0 (calcul récent), on ne doit pas le toucher."""
        import time
        now = time.time()
        db._conn.execute(
            "INSERT INTO samples_5min (ts, pv_avg, pv_kwh, load_kwh, samples) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, 1500, 0.5, 0.3, 30)
        )
        db._conn.commit()
        n = db._backfill_5min_kwh_columns()
        assert n == 0  # rien recalculé
        # Valeur originale préservée
        r = db._conn.execute("SELECT pv_kwh FROM samples_5min WHERE ts=?", (now,)).fetchone()
        assert r[0] == 0.5

    def test_repair_kwh_columns_returns_counts(self, db):
        """L'endpoint manuel doit fonctionner même sur DB vide."""
        r = db.repair_kwh_columns()
        assert r["status"] == "ok"
        assert "5min_recalculated" in r
        assert "hourly_rebuilt" in r
        assert "daily_recomputed" in r


# ═══════════════════════════════════════════════════════════════════════════
#  Tests Sprint 8 — Architecture inverters / Solax
# ═══════════════════════════════════════════════════════════════════════════

class TestInverterPlugin:
    """Tests de l'architecture base du package inverters."""

    def test_register_def_auto_length(self):
        from inverters.base import RegisterDef, RegisterType
        # U16 / S16 → length=1 auto
        r = RegisterDef(0x0010, RegisterType.U16)
        assert r.length == 1
        assert r.end_address == 0x0010
        # U32 / S32 → length=2
        r = RegisterDef(0x0020, RegisterType.U32)
        assert r.length == 2
        assert r.end_address == 0x0021

    def test_get_register_blocks_groups_neighbors(self):
        """Les registres voisins doivent être regroupés dans le même bloc."""
        from inverters.plugin_solax_x1 import SolaxX1HybridGen4
        plugin = SolaxX1HybridGen4(config={"host": "127.0.0.1"})
        blocks = plugin.get_register_blocks()
        assert len(blocks) > 0
        # Chaque bloc doit avoir une taille raisonnable (< 125)
        for start, count, regs, func in blocks:
            assert count <= 125
            assert count > 0
            assert len(regs) > 0

    def test_plugin_registry(self):
        from inverters import list_plugins, get_plugin
        plugins = list_plugins()
        assert "solax_x1_hybrid_gen4" in plugins
        assert "solax_x1_hybrid_gen3" in plugins
        cls = get_plugin("solax_x1_hybrid_gen4")
        assert cls is not None
        # Instance possible
        inst = cls(config={"host": "127.0.0.1"})
        assert inst.BRAND == "Solax"

    def test_unknown_plugin_returns_none(self):
        from inverters import get_plugin
        assert get_plugin("nonexistent_brand") is None


class TestModbusDecoding:
    """Vérifie que le décodage des registres bruts est correct."""

    def test_decode_u16(self):
        from inverters.modbus_client import ModbusInverterClient
        from inverters.base import RegisterDef, RegisterType
        reg = RegisterDef(0, RegisterType.U16, scale=0.1)
        # Valeur Modbus brute = 2305 → 230.5V (scaling 0.1)
        v = ModbusInverterClient._decode_registers([2305], reg)
        assert abs(v - 230.5) < 0.01

    def test_decode_s16_negative(self):
        from inverters.modbus_client import ModbusInverterClient
        from inverters.base import RegisterDef, RegisterType
        reg = RegisterDef(0, RegisterType.S16, scale=1)
        # 0xFFCE = -50 (signed 16-bit)
        v = ModbusInverterClient._decode_registers([0xFFCE], reg)
        assert v == -50

    def test_decode_u32(self):
        from inverters.modbus_client import ModbusInverterClient
        from inverters.base import RegisterDef, RegisterType
        reg = RegisterDef(0, RegisterType.U32, scale=0.1)
        # 0x00001234 → 4660 × 0.1 = 466.0
        v = ModbusInverterClient._decode_registers([0x0000, 0x1234], reg)
        assert abs(v - 466.0) < 0.01

    def test_decode_s32_negative(self):
        from inverters.modbus_client import ModbusInverterClient
        from inverters.base import RegisterDef, RegisterType
        reg = RegisterDef(0, RegisterType.S32, scale=1)
        # 0xFFFFFFFE = -2 (signed 32-bit)
        v = ModbusInverterClient._decode_registers([0xFFFF, 0xFFFE], reg)
        assert v == -2

    def test_decode_u32_lsb_solax(self):
        """Convention Solax : mot bas en premier (LSB-MSB).

        Validation contre données réelles du scan H4502T :
        registres 0x48-0x49 = [18974, 6] doivent donner 412187.
        """
        from inverters.modbus_client import ModbusInverterClient
        from inverters.base import RegisterDef, RegisterType
        reg = RegisterDef(0x48, RegisterType.U32_LSB, scale=0.01)
        v = ModbusInverterClient._decode_registers([18974, 6], reg)
        # (6 << 16) | 18974 = 412190 × 0.01 = 4121.90 kWh
        assert abs(v - 4121.90) < 0.01

    def test_decode_s32_lsb_negative(self):
        """S32_LSB doit gérer correctement les valeurs négatives."""
        from inverters.modbus_client import ModbusInverterClient
        from inverters.base import RegisterDef, RegisterType
        reg = RegisterDef(0x46, RegisterType.S32_LSB, scale=1)
        # -1500 W en S32 = 0xFFFFFA24, en LSB-MSB = [0xFA24, 0xFFFF]
        v = ModbusInverterClient._decode_registers([0xFA24, 0xFFFF], reg)
        assert v == -1500

    def test_decode_string(self):
        from inverters.modbus_client import ModbusInverterClient
        from inverters.base import RegisterDef, RegisterType
        reg = RegisterDef(0, RegisterType.STRING, length=3)
        # "AB" "CD" "EF" → "ABCDEF"
        registers = [0x4142, 0x4344, 0x4546]
        v = ModbusInverterClient._decode_registers(registers, reg)
        assert v == "ABCDEF"


class TestSolaxPlugin:
    """Tests du plugin Solax X1-Hybrid Gen4."""

    def test_detect_model_h4502(self):
        """Le serial H4502T... doit être identifié comme Gen4 5kW."""
        from inverters.plugin_solax_x1 import SolaxX1HybridGen4
        plugin = SolaxX1HybridGen4(config={"host": "127.0.0.1"})
        model = plugin.detect_model("H4502TI3474072")
        assert "5kw" in model.lower() or "gen4" in model.lower()

    def test_detect_model_short_serial(self):
        """Un serial trop court ne crash pas."""
        from inverters.plugin_solax_x1 import SolaxX1HybridGen4
        plugin = SolaxX1HybridGen4(config={"host": "127.0.0.1"})
        model = plugin.detect_model("AB")
        assert "unknown" in model.lower()

    def test_parse_status_minimal(self):
        """parse_status doit fonctionner même avec des registres manquants."""
        from inverters.plugin_solax_x1 import SolaxX1HybridGen4
        plugin = SolaxX1HybridGen4(config={"host": "127.0.0.1"})
        raw = {
            "serial": "H4502TI3474072",
            "pv1_power": 1500,
            "pv2_power": 800,
            "battery_soc": 85,
            "battery_power": -500,  # convention native Solax = négatif = charge → après invert = +500 (charge)
            "feedin_power": 200,    # 200W d'import (convention SEH = Solax)
            "yield_today": 12.5,
        }
        status = plugin.parse_status(raw)
        assert status.online is True
        assert status.serial == "H4502TI3474072"
        assert status.pv_power == 2300  # 1500 + 800
        # Convention SEH : grid_power = feedin_power direct
        assert status.grid_power == 200
        # battery_power inversé : raw=-500 → display=+500 (charge)
        assert status.battery_power == 500
        assert status.battery_soc == 85
        assert status.yield_today == 12.5

    def test_parse_status_export(self):
        """Si feedin_power est négatif (export Solax = export SEH), grid_power doit l'être aussi."""
        from inverters.plugin_solax_x1 import SolaxX1HybridGen4
        plugin = SolaxX1HybridGen4(config={"host": "127.0.0.1"})
        raw = {"feedin_power": -1500}  # 1500W exporté
        status = plugin.parse_status(raw)
        assert status.grid_power == -1500  # convention SEH : négatif = export

    def test_parse_status_battery_discharge(self):
        """Vérifie l'inversion de signe battery_power (Gen4)."""
        from inverters.plugin_solax_x1 import SolaxX1HybridGen4
        plugin = SolaxX1HybridGen4(config={"host": "127.0.0.1"})
        # Capture HA réelle : -78W décharge → reg natif Solax = +78
        raw = {"battery_power": 78, "battery_current": 0.2}
        status = plugin.parse_status(raw)
        assert status.battery_power == -78  # SEH : négatif = décharge
        assert abs(status.battery_current - (-0.2)) < 0.01

    def test_write_whitelist_protects(self):
        """WRITE_WHITELIST doit contenir les registres écriture autorisés."""
        from inverters.plugin_solax_x1 import SolaxX1HybridGen4
        plugin = SolaxX1HybridGen4(config={"host": "127.0.0.1"})
        assert "battery_min_capacity" in plugin.WRITE_WHITELIST
        spec = plugin.WRITE_WHITELIST["battery_min_capacity"]
        assert spec.min_value == 10
        assert spec.max_value == 100


class TestSolaxFleet:
    """Tests de la couche fleet Solax."""

    def test_disabled_by_default(self):
        from solax_fleet import SolaxFleet
        f = SolaxFleet({})
        assert not f.enabled

    def test_enabled_with_inverter(self):
        from solax_fleet import SolaxFleet
        f = SolaxFleet({
            "enabled": True,
            "inverters": [
                {"id": "test", "host": "127.0.0.1", "plugin_name": "solax_x1_hybrid_gen4"}
            ]
        })
        assert f.enabled
        assert f.poll_interval == 10

    def test_get_cache_initially_empty(self):
        from solax_fleet import SolaxFleet
        f = SolaxFleet({"enabled": True})
        assert f.get_cache() == {}


class TestSolaxAPI:
    def test_solax_endpoint(self, api_client):
        client, _ = api_client
        r = client.get("/api/solax")
        # Devrait répondre même si désactivé
        assert r.status_code == 200
        d = r.json()
        assert "enabled" in d


# ═══════════════════════════════════════════════════════════════════════════
#  Tests Sprint 11 — Persistance Solax (tables séparées)
# ═══════════════════════════════════════════════════════════════════════════

class TestSolaxPersistence:
    """Vérifie que les tables solax_* fonctionnent en parallèle de samples_*."""

    def test_solax_tables_exist(self, db):
        """Les 4 tables solax_* doivent être créées par le schema."""
        for table in ["solax_raw", "solax_5min", "solax_hourly", "solax_daily"]:
            r = db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,)
            ).fetchone()
            assert r is not None, f"Table {table} manquante"

    def test_insert_solax_sample(self, db):
        """Un sample inséré doit apparaître dans solax_raw."""
        db.insert_solax_sample(pv_power=1500, load_power=300, grid_power=-200,
                                bat_power=400, bat_soc=85, bat_voltage=295,
                                frequency=49.97, temperature=34)
        n = db._conn.execute("SELECT COUNT(*) FROM solax_raw").fetchone()[0]
        assert n == 1
        row = db._conn.execute(
            "SELECT pv_power, load_power, bat_soc FROM solax_raw"
        ).fetchone()
        assert row[0] == 1500
        assert row[1] == 300
        assert row[2] == 85

    def test_solax_does_not_pollute_victron_tables(self, db):
        """Insérer dans solax_* ne doit RIEN écrire dans samples_*."""
        # Compte avant
        n_before = db._conn.execute("SELECT COUNT(*) FROM samples_raw").fetchone()[0]
        # Insère 5 samples Solax
        for i in range(5):
            db.insert_solax_sample(pv_power=1000, bat_soc=80)
        # Compte après dans samples_raw → inchangé
        n_after = db._conn.execute("SELECT COUNT(*) FROM samples_raw").fetchone()[0]
        assert n_after == n_before
        # Mais solax_raw a bien 5 entrées
        n_solax = db._conn.execute("SELECT COUNT(*) FROM solax_raw").fetchone()[0]
        assert n_solax == 5

    def test_solax_unique_index_5min(self, db):
        """L'UNIQUE INDEX sur solax_5min doit empêcher les doublons (ON CONFLICT)."""
        import time
        ts = time.time()
        # Insère deux fois sur le même ts → le 2e doit ÉCRASER le 1er
        db._conn.execute(
            "INSERT INTO solax_5min (ts, pv_avg, samples) VALUES (?, ?, ?) "
            "ON CONFLICT(ts) DO UPDATE SET pv_avg=excluded.pv_avg, samples=excluded.samples",
            (ts, 100, 30)
        )
        db._conn.execute(
            "INSERT INTO solax_5min (ts, pv_avg, samples) VALUES (?, ?, ?) "
            "ON CONFLICT(ts) DO UPDATE SET pv_avg=excluded.pv_avg, samples=excluded.samples",
            (ts, 200, 60)
        )
        db._conn.commit()
        rows = db._conn.execute(
            "SELECT pv_avg, samples FROM solax_5min WHERE ts=?", (ts,)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 200  # écrasement OK
        assert rows[0][1] == 60

    def test_get_solax_realtime_empty(self, db):
        """get_solax_realtime sur DB vide retourne liste vide."""
        r = db.get_solax_realtime(hours=24)
        assert r == []

    def test_get_solax_realtime_with_data(self, db):
        """Insertion + lecture via get_solax_realtime."""
        # Insère un sample 5min directement (simule l'agrégation déjà faite)
        import time
        now = time.time()
        db._conn.execute(
            "INSERT INTO solax_5min (ts, pv_avg, load_avg, grid_avg, bat_avg, soc_avg, samples) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now - 60, 1500, 400, -300, 200, 87, 30)
        )
        db._conn.commit()
        r = db.get_solax_realtime(hours=24)
        assert len(r) == 1
        assert r[0]["pv"] == 1500
        assert r[0]["soc"] == 87


class TestSolaxHistoryAPI:
    def test_solax_history_endpoint(self, api_client):
        client, _ = api_client
        r = client.get("/api/solax/history?range=24h")
        assert r.status_code == 200
        d = r.json()
        assert "data" in d
        assert d["range"] == "24h"

    def test_solax_history_invalid_range(self, api_client):
        client, _ = api_client
        r = client.get("/api/solax/history?range=99x")
        assert r.status_code == 200
        d = r.json()
        assert "error" in d


class TestSolaxFeedinDecoding:
    """Décodage du registre measured_power (0x46) en S32_LSB-MSB sur firmware Alexis."""

    def test_feedin_s32_lsb_negative(self):
        """Cas réel : registres [0xFFC3, 0xFFFF] = -61W d'export en S32_LSB."""
        from inverters.modbus_client import ModbusInverterClient
        from inverters.base import RegisterDef, RegisterType
        reg = RegisterDef(0x46, RegisterType.S32_LSB, scale=1)
        # Registres bruts capturés : 0xFFC3, 0xFFFF
        # En S32_LSB : (0xFFFF << 16) | 0xFFC3 = 0xFFFFFFC3 = -61 (signed 32)
        v = ModbusInverterClient._decode_registers([0xFFC3, 0xFFFF], reg)
        assert v == -61, f"Attendu -61, obtenu {v}"

    def test_feedin_s32_lsb_positive(self):
        """Cas import 100W : registre = [100, 0]."""
        from inverters.modbus_client import ModbusInverterClient
        from inverters.base import RegisterDef, RegisterType
        reg = RegisterDef(0x46, RegisterType.S32_LSB, scale=1)
        v = ModbusInverterClient._decode_registers([100, 0], reg)
        assert v == 100

    def test_feedin_passes_through_to_grid_power(self):
        """parse_status doit transmettre feedin_power → grid_power direct."""
        from inverters.plugin_solax_x1 import SolaxX1HybridGen4
        plugin = SolaxX1HybridGen4(config={"host": "127.0.0.1"})
        status = plugin.parse_status({"feedin_power": -61, "inverter_power": 0})
        # Convention SEH : - = export (cohérent avec ce que voit HA)
        assert status.grid_power == -61
        # load = max(0, 0 + -61) = 0 (rien dans la maison, on exporte tout)
        assert status.load_power == 0

    def test_feedin_import_typical(self):
        """Cas typique en journée : import 60W, onduleur fournit 99W → maison 159W."""
        from inverters.plugin_solax_x1 import SolaxX1HybridGen4
        plugin = SolaxX1HybridGen4(config={"host": "127.0.0.1"})
        status = plugin.parse_status({"feedin_power": 60, "inverter_power": 99})
        assert status.grid_power == 60
        assert status.load_power == 159
