"""
Smart Energy Hub — SQLite Time-Series Storage
Tables :
  samples_raw     : échantillons bruts (~10s), rétention 24h
  samples_5min    : agrégats 5 min, rétention 30 jours
  samples_hourly  : agrégats horaires, rétention 12 mois
  samples_daily   : agrégats journaliers, rétention illimitée

Chaque agrégat stocke : min, max, avg pour chaque métrique.
"""
import asyncio
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/smart_energy_hub.db")

# Rétention en secondes
RETENTION_RAW     = 24 * 3600        # 24h
RETENTION_5MIN    = 30 * 24 * 3600   # 30 jours
RETENTION_HOURLY  = 365 * 24 * 3600  # 12 mois
# daily = pas de rétention

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples_raw (
    ts         REAL NOT NULL,
    pv_power   REAL DEFAULT 0,
    load_power REAL DEFAULT 0,
    grid_power REAL DEFAULT 0,
    bat_power  REAL DEFAULT 0,
    bat_soc    REAL DEFAULT 0,
    bat_voltage REAL DEFAULT 0,
    frequency  REAL DEFAULT 0,
    temperature REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_raw_ts ON samples_raw(ts);

CREATE TABLE IF NOT EXISTS samples_5min (
    ts         REAL NOT NULL,
    pv_avg     REAL DEFAULT 0, pv_max    REAL DEFAULT 0,
    load_avg   REAL DEFAULT 0, load_max  REAL DEFAULT 0,
    grid_avg   REAL DEFAULT 0, grid_min  REAL DEFAULT 0, grid_max REAL DEFAULT 0,
    bat_avg    REAL DEFAULT 0, bat_min   REAL DEFAULT 0, bat_max  REAL DEFAULT 0,
    soc_avg    REAL DEFAULT 0, soc_min   REAL DEFAULT 0, soc_max  REAL DEFAULT 0,
    pv_kwh     REAL DEFAULT 0, load_kwh  REAL DEFAULT 0,
    import_kwh REAL DEFAULT 0, export_kwh REAL DEFAULT 0,
    bat_charge_kwh REAL DEFAULT 0, bat_discharge_kwh REAL DEFAULT 0,
    samples    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_5min_ts ON samples_5min(ts);
-- v3 : l'UNIQUE INDEX sera créé par _migrate APRÈS dédoublonnage si nécessaire
-- (créer ici poserait souci sur les anciennes DB qui contiennent déjà des doublons)

CREATE TABLE IF NOT EXISTS samples_hourly (
    ts         REAL NOT NULL,
    pv_avg     REAL DEFAULT 0, pv_max    REAL DEFAULT 0,
    load_avg   REAL DEFAULT 0, load_max  REAL DEFAULT 0,
    grid_avg   REAL DEFAULT 0, grid_min  REAL DEFAULT 0, grid_max REAL DEFAULT 0,
    bat_avg    REAL DEFAULT 0, bat_min   REAL DEFAULT 0, bat_max  REAL DEFAULT 0,
    soc_avg    REAL DEFAULT 0, soc_min   REAL DEFAULT 0, soc_max  REAL DEFAULT 0,
    pv_kwh     REAL DEFAULT 0, load_kwh  REAL DEFAULT 0,
    import_kwh REAL DEFAULT 0, export_kwh REAL DEFAULT 0,
    bat_charge_kwh REAL DEFAULT 0, bat_discharge_kwh REAL DEFAULT 0,
    samples    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_hourly_ts ON samples_hourly(ts);
-- UNIQUE INDEX géré par _migrate après dédoublonnage

CREATE TABLE IF NOT EXISTS samples_daily (
    ts          REAL NOT NULL,
    date_str    TEXT NOT NULL,
    pv_avg      REAL DEFAULT 0, pv_max     REAL DEFAULT 0,
    load_avg    REAL DEFAULT 0, load_max   REAL DEFAULT 0,
    grid_avg    REAL DEFAULT 0,
    soc_avg     REAL DEFAULT 0, soc_min    REAL DEFAULT 0, soc_max REAL DEFAULT 0,
    pv_kwh      REAL DEFAULT 0, load_kwh   REAL DEFAULT 0,
    import_kwh  REAL DEFAULT 0, export_kwh REAL DEFAULT 0,
    bat_charge_kwh REAL DEFAULT 0, bat_discharge_kwh REAL DEFAULT 0,
    self_sufficiency REAL DEFAULT 0,
    samples     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_daily_ts ON samples_daily(ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_date ON samples_daily(date_str);

-- ═══════════════════════════════════════════════════════════════════════
--  Sprint 11 : tables Solax en parallèle de Victron
-- ═══════════════════════════════════════════════════════════════════════
-- Mêmes structures que samples_*, juste préfixées solax_*. Permet à un même
-- déploiement d'avoir Victron ET Solax sans conflit (utile chez Alexis qui
-- a les deux). Conventions de signe : grid_power positif=import, bat_power
-- positif=charge (cohérent avec Victron).

CREATE TABLE IF NOT EXISTS solax_raw (
    ts          REAL NOT NULL,
    pv_power    REAL DEFAULT 0,
    load_power  REAL DEFAULT 0,
    grid_power  REAL DEFAULT 0,
    bat_power   REAL DEFAULT 0,
    bat_soc     REAL DEFAULT 0,
    bat_voltage REAL DEFAULT 0,
    frequency   REAL DEFAULT 0,
    temperature REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_solax_raw_ts ON solax_raw(ts);

CREATE TABLE IF NOT EXISTS solax_5min (
    ts         REAL NOT NULL,
    pv_avg     REAL DEFAULT 0, pv_max    REAL DEFAULT 0,
    load_avg   REAL DEFAULT 0, load_max  REAL DEFAULT 0,
    grid_avg   REAL DEFAULT 0, grid_min  REAL DEFAULT 0, grid_max REAL DEFAULT 0,
    bat_avg    REAL DEFAULT 0, bat_min   REAL DEFAULT 0, bat_max  REAL DEFAULT 0,
    soc_avg    REAL DEFAULT 0, soc_min   REAL DEFAULT 0, soc_max  REAL DEFAULT 0,
    pv_kwh     REAL DEFAULT 0, load_kwh  REAL DEFAULT 0,
    import_kwh REAL DEFAULT 0, export_kwh REAL DEFAULT 0,
    bat_charge_kwh REAL DEFAULT 0, bat_discharge_kwh REAL DEFAULT 0,
    samples    INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_solax_5min_ts ON solax_5min(ts);

CREATE TABLE IF NOT EXISTS solax_hourly (
    ts         REAL NOT NULL,
    pv_avg     REAL DEFAULT 0, pv_max    REAL DEFAULT 0,
    load_avg   REAL DEFAULT 0, load_max  REAL DEFAULT 0,
    grid_avg   REAL DEFAULT 0, grid_min  REAL DEFAULT 0, grid_max REAL DEFAULT 0,
    bat_avg    REAL DEFAULT 0, bat_min   REAL DEFAULT 0, bat_max  REAL DEFAULT 0,
    soc_avg    REAL DEFAULT 0, soc_min   REAL DEFAULT 0, soc_max  REAL DEFAULT 0,
    pv_kwh     REAL DEFAULT 0, load_kwh  REAL DEFAULT 0,
    import_kwh REAL DEFAULT 0, export_kwh REAL DEFAULT 0,
    bat_charge_kwh REAL DEFAULT 0, bat_discharge_kwh REAL DEFAULT 0,
    samples    INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_solax_hourly_ts ON solax_hourly(ts);

CREATE TABLE IF NOT EXISTS solax_daily (
    ts          REAL NOT NULL,
    date_str    TEXT NOT NULL,
    pv_avg      REAL DEFAULT 0, pv_max     REAL DEFAULT 0,
    load_avg    REAL DEFAULT 0, load_max   REAL DEFAULT 0,
    grid_avg    REAL DEFAULT 0,
    soc_avg     REAL DEFAULT 0, soc_min    REAL DEFAULT 0, soc_max REAL DEFAULT 0,
    pv_kwh      REAL DEFAULT 0, load_kwh   REAL DEFAULT 0,
    import_kwh  REAL DEFAULT 0, export_kwh REAL DEFAULT 0,
    bat_charge_kwh REAL DEFAULT 0, bat_discharge_kwh REAL DEFAULT 0,
    self_sufficiency REAL DEFAULT 0,
    samples     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_solax_daily_ts ON solax_daily(ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_solax_daily_date ON solax_daily(date_str);

-- Sprint 6 : snapshots de prévision solaire pour mesurer la précision
-- Chaque entrée = une prévision faite à `snapshot_ts` pour le jour `forecast_date`
-- Idempotence : (snapshot_date, forecast_date) unique → on écrase la dernière prévision du jour
CREATE TABLE IF NOT EXISTS solar_forecast_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_ts    REAL NOT NULL,
    snapshot_date  TEXT NOT NULL,   -- "YYYY-MM-DD" de quand on a fait la prévision
    forecast_date  TEXT NOT NULL,   -- "YYYY-MM-DD" du jour prévu
    forecast_kwh   REAL NOT NULL,
    horizon_days   INTEGER DEFAULT 0,  -- 0 = prévision du jour J pour J, 1 = J pour J+1, etc.
    created_at     REAL DEFAULT (strftime('%s','now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fcsnap_pair
    ON solar_forecast_snapshots(snapshot_date, forecast_date);
CREATE INDEX IF NOT EXISTS idx_fcsnap_forecast_date
    ON solar_forecast_snapshots(forecast_date);
"""


class EnergyDatabase:
    """Gestionnaire de base de données SQLite pour les métriques d'énergie."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._conn: sqlite3.Connection = None
        self._last_5min_ts: float = 0
        self._last_hourly_ts: float = 0
        self._last_daily_ts: float = 0
        self._last_cleanup_ts: float = 0
        # Sprint 11 : compteurs Solax séparés
        self._last_solax_5min_ts: float = 0
        self._last_solax_hourly_ts: float = 0
        self._last_solax_daily_ts: float = 0
        self._last_solax_cleanup_ts: float = 0

    def init(self):
        """Initialise la DB et crée les tables."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate()
        logger.info("SQLite initialisée : %s", self.db_path)

    def _migrate(self):
        """Migrations idempotentes pour les DB existantes."""
        # ── v2 : colonnes kWh ajoutées dans samples_5min ──
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(samples_5min)").fetchall()]
        new_cols = [
            ("pv_kwh", "REAL DEFAULT 0"),
            ("load_kwh", "REAL DEFAULT 0"),
            ("import_kwh", "REAL DEFAULT 0"),
            ("export_kwh", "REAL DEFAULT 0"),
            ("bat_charge_kwh", "REAL DEFAULT 0"),
            ("bat_discharge_kwh", "REAL DEFAULT 0"),
        ]
        migrated = False
        for name, spec in new_cols:
            if name not in cols:
                try:
                    self._conn.execute(f"ALTER TABLE samples_5min ADD COLUMN {name} {spec}")
                    migrated = True
                except Exception as e:
                    logger.debug(f"Migration {name}: {e}")
        if migrated:
            self._conn.commit()
            logger.info("DB migration v2 appliquée (colonnes kWh dans samples_5min)")

        # ── v3 : dédoublonnage des agrégats + création UNIQUE INDEX sur ts ──
        # Bug : avant Sprint 7, _aggregate_5min/_aggregate_hourly insérait sans
        # vérifier les doublons. Le recompute_recent ou un redémarrage rapide
        # pouvait créer plusieurs lignes pour le même ts → totaux gonflés.
        for table in ("samples_5min", "samples_hourly"):
            idx_name = f"idx_{table.replace('samples_', '')}_ts_unique"
            existing_idx = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                (idx_name,)
            ).fetchone()
            if existing_idx:
                continue  # Index déjà créé → migration v3 déjà appliquée

            # Compter les doublons
            n_dup = self._conn.execute(
                f"SELECT COUNT(*) FROM (SELECT ts FROM {table} GROUP BY ts HAVING COUNT(*) > 1)"
            ).fetchone()[0]

            if n_dup > 0:
                logger.warning(
                    f"Migration v3: {table} contient {n_dup} ts dupliqués. "
                    f"Dédoublonnage automatique en cours..."
                )
                # Dédoublonner : garder la première ligne pour chaque ts
                self._conn.execute(f"""
                    DELETE FROM {table}
                    WHERE rowid NOT IN (
                        SELECT MIN(rowid) FROM {table} GROUP BY ts
                    )
                """)
                self._conn.commit()
                logger.info(f"Migration v3: {table} dédoublonné")

            # Créer l'UNIQUE INDEX (maintenant safe)
            try:
                self._conn.execute(f"CREATE UNIQUE INDEX {idx_name} ON {table}(ts)")
                self._conn.commit()
                logger.info(f"Migration v3: UNIQUE INDEX créé sur {table}.ts")
            except Exception as e:
                logger.warning(f"Création UNIQUE INDEX {idx_name} échouée: {e}")

        # Si on a dédoublonné samples_hourly, recomputer toutes les samples_daily
        # car elles étaient calculées sur des hourly avec doublons → totaux faux
        v3_dedupe_done = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_hourly_ts_unique'"
        ).fetchone()
        v3_recompute_done = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE name='_seh_v3_daily_recomputed' "
            "AND type='table'"
        ).fetchone()
        if v3_dedupe_done and not v3_recompute_done:
            logger.info("Migration v3: recompute des samples_daily depuis samples_hourly nettoyés...")
            n = self._recompute_all_daily_from_hourly()
            # Marqueur pour ne pas refaire à chaque démarrage
            self._conn.execute("CREATE TABLE IF NOT EXISTS _seh_v3_daily_recomputed (done INTEGER)")
            self._conn.execute("INSERT INTO _seh_v3_daily_recomputed (done) VALUES (1)")
            self._conn.commit()
            logger.info(f"Migration v3: {n} samples_daily recomputés")

        # ── v4 : recalcul rétroactif des kWh dans samples_5min ──
        # Bug : avant Sprint 4, les colonnes pv_kwh/load_kwh/import_kwh/export_kwh
        # n'existaient pas dans samples_5min. Après leur ajout (migration v2), les
        # rows existants étaient initialisés à 0. L'agrégation hourly basée sur
        # SUM(pv_kwh) renvoyait donc 0 pour les jours antérieurs au sprint 4.
        # Pire : un bug d'inversion de colonnes a fait que pv_avg (W) s'est retrouvé
        # dans pv_kwh des hourly → SUM = 24×350W = 8000 kWh affiché en daily.
        v4_done = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE name='_seh_v4_kwh_backfilled' "
            "AND type='table'"
        ).fetchone()
        if not v4_done:
            n_recalc = self._backfill_5min_kwh_columns()
            if n_recalc > 0:
                # Une fois 5min réparés, il faut reconstruire hourly + daily
                logger.info(f"Migration v4: {n_recalc} samples_5min recalculés. "
                            f"Reconstruction hourly et daily...")
                n_h = self._rebuild_hourly_from_5min()
                n_d = self._recompute_all_daily_from_hourly()
                logger.info(f"Migration v4: {n_h} hourly + {n_d} daily reconstruits")
            self._conn.execute("CREATE TABLE IF NOT EXISTS _seh_v4_kwh_backfilled (done INTEGER)")
            self._conn.execute("INSERT INTO _seh_v4_kwh_backfilled (done) VALUES (1)")
            self._conn.commit()

        # ── v5 (Sprint 10) : table users + _seh_meta + flag first_boot ──
        # Système d'authentification multi-utilisateurs (admin/user) avec bcrypt + JWT.
        # Le flag first_boot pilote l'affichage du wizard d'onboarding au 1er démarrage.
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin','user')),
                created_at INTEGER NOT NULL,
                last_login INTEGER
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS _seh_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)
        # Initialiser first_boot uniquement si la clé n'existe pas (1ère install)
        exists = self._conn.execute(
            "SELECT 1 FROM _seh_meta WHERE key=?", ("first_boot",)
        ).fetchone()
        if not exists:
            # DB neuve = first_boot=true, DB existante migrée = first_boot=false
            has_data = self._conn.execute(
                "SELECT 1 FROM samples_daily LIMIT 1"
            ).fetchone()
            initial = "false" if has_data else "true"
            self._conn.execute(
                "INSERT INTO _seh_meta (key, value, updated_at) VALUES (?, ?, ?)",
                ("first_boot", initial, int(time.time())),
            )
            logger.info("Migration v5: first_boot=%s (DB %s)",
                        initial, "existante" if has_data else "neuve")
        self._conn.commit()

    # ════════════════════════════════════════════════════════════════════════
    #  Sprint 10 : Helpers meta + flag first_boot + reset admin
    # ════════════════════════════════════════════════════════════════════════

    @property
    def conn(self):
        """Accès brut à la connexion SQLite (utilisé par AuthManager)."""
        return self._conn

    def get_meta(self, key: str, default=None):
        """Lit une valeur dans la table _seh_meta."""
        r = self._conn.execute("SELECT value FROM _seh_meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key: str, value: str):
        """Écrit une valeur dans la table _seh_meta (upsert)."""
        self._conn.execute("""
            INSERT INTO _seh_meta (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """, (key, value, int(time.time())))
        self._conn.commit()

    def is_first_boot(self) -> bool:
        return self.get_meta("first_boot", "false") == "true"

    def mark_first_boot_complete(self):
        self.set_meta("first_boot", "false")

    def check_and_apply_reset_flag(self) -> bool:
        """
        Détecte /data/reset.flag et, s'il existe, supprime tous les users +
        remet first_boot=true. Procédure de récupération en cas de mot de
        passe admin oublié.
        """
        flag_path = Path(self.db_path).parent / "reset.flag"
        if not flag_path.exists():
            return False
        logger.warning("Fichier reset.flag détecté → suppression de tous les users + first_boot=true")
        self._conn.execute("DELETE FROM users")
        self.set_meta("first_boot", "true")
        try:
            flag_path.unlink()
        except OSError as e:
            logger.error("Impossible de supprimer reset.flag: %s", e)
        logger.warning("Reset admin appliqué — wizard redémarrera")
        return True

    def _backfill_5min_kwh_columns(self) -> int:
        """
        Pour chaque row samples_5min où pv_kwh=0 mais qu'il y a quand même de
        l'activité (pv_avg > 0 ou load_avg > 0), recalcule les colonnes kWh
        depuis les puissances moyennes : kWh = P_moy [W] × (5/60h) / 1000.

        Approximation pour import/export : utilise grid_avg signé. Moins précis
        que le CASE WHEN sur samples_raw (qui ne sont plus disponibles pour les
        jours anciens), mais infiniment mieux que 0.
        """
        rows = self._conn.execute("""
            SELECT ts, pv_avg, load_avg, grid_avg, bat_avg
            FROM samples_5min
            WHERE pv_kwh = 0 AND (pv_avg > 0 OR load_avg > 0 OR ABS(grid_avg) > 0)
        """).fetchall()
        if not rows:
            return 0

        H = 5.0 / 60.0 / 1000.0  # facteur P_moy [W] → kWh sur 5 min
        for ts, pv_avg, load_avg, grid_avg, bat_avg in rows:
            pv_avg = pv_avg or 0
            load_avg = load_avg or 0
            grid_avg = grid_avg or 0
            bat_avg = bat_avg or 0
            self._conn.execute(
                "UPDATE samples_5min SET "
                "pv_kwh = ?, load_kwh = ?, "
                "import_kwh = ?, export_kwh = ?, "
                "bat_charge_kwh = ?, bat_discharge_kwh = ? "
                "WHERE ts = ?",
                (round(pv_avg * H, 4),
                 round(load_avg * H, 4),
                 round(max(0, grid_avg) * H, 4),
                 round(abs(min(0, grid_avg)) * H, 4),
                 round(max(0, bat_avg) * H, 4),
                 round(abs(min(0, bat_avg)) * H, 4),
                 ts)
            )
        self._conn.commit()
        return len(rows)

    def _rebuild_hourly_from_5min(self) -> int:
        """
        Reconstruit entièrement la table samples_hourly à partir des samples_5min.
        Utilisé par migration v4 après backfill des kWh, ou manuellement via
        l'endpoint /api/maintenance/repair_kwh.
        """
        # Vide la table
        self._conn.execute("DELETE FROM samples_hourly")
        mm = self._conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM samples_5min"
        ).fetchone()
        if not mm or mm[0] is None:
            return 0
        t_min, t_max = mm
        # Aligner sur début d'heure pour avoir des slots cohérents
        t = (int(t_min) // 3600 + 1) * 3600
        n = 0
        while t <= t_max:
            row = self._conn.execute("""
                SELECT
                    AVG(pv_avg), MAX(pv_max),
                    AVG(load_avg), MAX(load_max),
                    AVG(grid_avg), MIN(grid_min), MAX(grid_max),
                    AVG(bat_avg), MIN(bat_min), MAX(bat_max),
                    AVG(soc_avg), MIN(soc_min), MAX(soc_max),
                    SUM(samples),
                    SUM(pv_kwh), SUM(load_kwh),
                    SUM(import_kwh), SUM(export_kwh),
                    SUM(bat_charge_kwh), SUM(bat_discharge_kwh)
                FROM samples_5min WHERE ts > ? AND ts <= ?
            """, (t - 3600, t)).fetchone()
            if row and row[13] and row[13] > 0:
                self._conn.execute(
                    "INSERT INTO samples_hourly (ts, pv_avg, pv_max, load_avg, load_max, "
                    "grid_avg, grid_min, grid_max, bat_avg, bat_min, bat_max, "
                    "soc_avg, soc_min, soc_max, "
                    "pv_kwh, load_kwh, import_kwh, export_kwh, "
                    "bat_charge_kwh, bat_discharge_kwh, samples) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (t, *row[:13],
                     round(row[14] or 0, 4), round(row[15] or 0, 4),
                     round(row[16] or 0, 4), round(row[17] or 0, 4),
                     round(row[18] or 0, 4), round(row[19] or 0, 4),
                     row[13])
                )
                n += 1
            t += 3600
        self._conn.commit()
        return n

    def repair_kwh_columns(self):
        """Endpoint manuel pour relancer le backfill kWh + reconstruction hourly+daily."""
        n_5min = self._backfill_5min_kwh_columns()
        n_hourly = self._rebuild_hourly_from_5min()
        n_daily = self._recompute_all_daily_from_hourly()
        return {
            "status": "ok",
            "5min_recalculated": n_5min,
            "hourly_rebuilt": n_hourly,
            "daily_recomputed": n_daily,
        }

    def _recompute_all_daily_from_hourly(self) -> int:
        """Reconstruit la table samples_daily à partir de samples_hourly nettoyé."""
        import datetime as dt
        # Toutes les dates qui ont au moins 1 row hourly
        affected = self._conn.execute("""
            SELECT DISTINCT date(datetime(ts, 'unixepoch', 'localtime')) AS d
            FROM samples_hourly
            ORDER BY d
        """).fetchall()
        n = 0
        for (date_str,) in affected:
            if not date_str:
                continue
            row = self._conn.execute("""
                SELECT
                    AVG(pv_avg), MAX(pv_max),
                    AVG(load_avg), MAX(load_max),
                    AVG(grid_avg),
                    AVG(soc_avg), MIN(soc_min), MAX(soc_max),
                    SUM(pv_kwh), SUM(load_kwh),
                    SUM(import_kwh), SUM(export_kwh),
                    SUM(bat_charge_kwh), SUM(bat_discharge_kwh),
                    SUM(samples)
                FROM samples_hourly
                WHERE date(datetime(ts, 'unixepoch', 'localtime')) = ?
            """, (date_str,)).fetchone()
            if not row or not row[14]:
                continue
            load_kwh = row[9] or 0
            import_kwh = row[10] or 0
            self_suff = round(((load_kwh - import_kwh) / load_kwh * 100)
                              if load_kwh > 0 else 0, 1)
            day_ts = dt.datetime.strptime(date_str, "%Y-%m-%d").timestamp()
            self._conn.execute("""
                INSERT INTO samples_daily
                    (ts, date_str, pv_avg, pv_max, load_avg, load_max, grid_avg,
                     soc_avg, soc_min, soc_max,
                     pv_kwh, load_kwh, import_kwh, export_kwh,
                     bat_charge_kwh, bat_discharge_kwh,
                     self_sufficiency, samples)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date_str) DO UPDATE SET
                    ts=excluded.ts, pv_avg=excluded.pv_avg, pv_max=excluded.pv_max,
                    load_avg=excluded.load_avg, load_max=excluded.load_max,
                    grid_avg=excluded.grid_avg,
                    soc_avg=excluded.soc_avg, soc_min=excluded.soc_min, soc_max=excluded.soc_max,
                    pv_kwh=excluded.pv_kwh, load_kwh=excluded.load_kwh,
                    import_kwh=excluded.import_kwh, export_kwh=excluded.export_kwh,
                    bat_charge_kwh=excluded.bat_charge_kwh, bat_discharge_kwh=excluded.bat_discharge_kwh,
                    self_sufficiency=excluded.self_sufficiency, samples=excluded.samples
            """, (day_ts, date_str, row[0] or 0, row[1] or 0, row[2] or 0, row[3] or 0,
                  row[4] or 0, row[5] or 0, row[6] or 0, row[7] or 0,
                  round(row[8] or 0, 3), round(load_kwh, 3),
                  round(import_kwh, 3), round(row[11] or 0, 3),
                  round(row[12] or 0, 3), round(row[13] or 0, 3),
                  self_suff, row[14]))
            n += 1
        self._conn.commit()
        return n

    def recompute_recent(self):
        """
        Recompute des agrégats 5min → hourly → daily à partir des samples_raw disponibles.
        Utile après migration pour corriger import_kwh/export_kwh sur les ~24h rattrapables.
        Note : les samples_raw ont une rétention 24h, on ne peut pas remonter plus loin.
        """
        if not self._conn:
            return {"status": "error", "message": "DB not initialized"}
        now = time.time()
        # 1) Purger les 5min et hourly récents (qu'on va reconstruire proprement)
        self._conn.execute("DELETE FROM samples_5min WHERE ts > ?", (now - 86400,))
        self._conn.execute("DELETE FROM samples_hourly WHERE ts > ?", (now - 86400,))

        # 2) Reconstruire 5min tranche par tranche
        rows_raw = self._conn.execute("SELECT MIN(ts), MAX(ts) FROM samples_raw").fetchone()
        if not rows_raw or rows_raw[0] is None:
            self._conn.commit()
            return {"status": "ok", "message": "Aucun sample brut à recomputer",
                    "slots_5min": 0, "slots_hourly": 0}
        t_min, t_max = rows_raw
        t = t_min + 300
        n5 = 0
        while t <= t_max:
            self._aggregate_5min(t)
            t += 300
            n5 += 1

        # 3) Reconstruire hourly
        t = t_min + 3600
        nh = 0
        while t <= t_max:
            self._aggregate_hourly(t)
            t += 3600
            nh += 1

        # 4) Reconstruire daily pour aujourd'hui (si dans le range)
        self._aggregate_daily(now)

        self._conn.commit()
        logger.info("Recompute terminé: %d slots 5min, %d slots hourly", n5, nh)
        return {"status": "ok", "slots_5min": n5, "slots_hourly": nh}

    def dedupe_aggregates(self, dry_run: bool = False):
        """
        Réparation d'une DB qui contient des doublons sur samples_5min / samples_hourly
        à cause du bug d'absence d'UNIQUE constraint sur ts (corrigé Sprint 7+).

        Stratégie :
          - Détecte les ts dupliqués
          - Garde le MIN de chaque colonne kWh (la valeur "originelle" non polluée)
          - Supprime les doublons
          - Re-agrège les samples_daily depuis les samples_hourly nettoyés
        """
        if not self._conn:
            return {"status": "error", "message": "DB not initialized"}

        report = {"5min": {}, "hourly": {}, "daily_recomputed": 0}

        for table in ("samples_5min", "samples_hourly"):
            # Compter les doublons
            dups = self._conn.execute(
                f"SELECT ts, COUNT(*) c FROM {table} GROUP BY ts HAVING c > 1"
            ).fetchall()
            n_dup_groups = len(dups)
            n_extra_rows = sum(d[1] - 1 for d in dups)

            report[table.replace("samples_", "")] = {
                "duplicate_ts_groups": n_dup_groups,
                "extra_rows_to_remove": n_extra_rows,
            }

            if not dry_run and n_dup_groups > 0:
                # Pour chaque ts dupliqué : créer une ligne "agrégée" avec les MIN
                # puis supprimer toutes les anciennes
                for (ts, count) in dups:
                    # Récupère les valeurs min (ou les premières) de chaque ts
                    consol = self._conn.execute(f"""
                        SELECT
                            MIN(pv_avg), MIN(pv_max), MIN(load_avg), MIN(load_max),
                            AVG(grid_avg), MIN(grid_min), MAX(grid_max),
                            AVG(bat_avg), MIN(bat_min), MAX(bat_max),
                            AVG(soc_avg), MIN(soc_min), MAX(soc_max),
                            MIN(pv_kwh), MIN(load_kwh),
                            MIN(import_kwh), MIN(export_kwh),
                            MIN(bat_charge_kwh), MIN(bat_discharge_kwh),
                            MIN(samples)
                        FROM {table} WHERE ts = ?
                    """, (ts,)).fetchone()

                    # Supprime toutes les lignes pour ce ts
                    self._conn.execute(f"DELETE FROM {table} WHERE ts = ?", (ts,))

                    # Réinsère une seule ligne consolidée
                    self._conn.execute(
                        f"INSERT INTO {table} (ts, pv_avg, pv_max, load_avg, load_max, "
                        f"grid_avg, grid_min, grid_max, bat_avg, bat_min, bat_max, "
                        f"soc_avg, soc_min, soc_max, "
                        f"pv_kwh, load_kwh, import_kwh, export_kwh, "
                        f"bat_charge_kwh, bat_discharge_kwh, samples) "
                        f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (ts, *consol)
                    )
                logger.info(f"Dédoublonné {table} : {n_dup_groups} groupes, {n_extra_rows} lignes en trop")

        # Recomputer les samples_daily affectés
        if not dry_run and (report["5min"]["duplicate_ts_groups"] > 0
                            or report["hourly"]["duplicate_ts_groups"] > 0):
            # Récupère toutes les dates impactées (depuis hourly)
            affected_dates = self._conn.execute("""
                SELECT DISTINCT date(datetime(ts, 'unixepoch', 'localtime'))
                FROM samples_hourly
            """).fetchall()

            import datetime as dt
            for (date_str,) in affected_dates:
                if not date_str:
                    continue
                # Recalcule cette journée
                row = self._conn.execute("""
                    SELECT
                        AVG(pv_avg), MAX(pv_max),
                        AVG(load_avg), MAX(load_max),
                        AVG(grid_avg),
                        AVG(soc_avg), MIN(soc_min), MAX(soc_max),
                        SUM(pv_kwh), SUM(load_kwh),
                        SUM(import_kwh), SUM(export_kwh),
                        SUM(bat_charge_kwh), SUM(bat_discharge_kwh),
                        SUM(samples)
                    FROM samples_hourly
                    WHERE date(datetime(ts, 'unixepoch', 'localtime')) = ?
                """, (date_str,)).fetchone()

                if row and row[14] and row[14] > 0:
                    load_kwh = row[9] or 0
                    import_kwh = row[10] or 0
                    self_suff = round(((load_kwh - import_kwh) / load_kwh * 100)
                                      if load_kwh > 0 else 0, 1)
                    day_ts = dt.datetime.strptime(date_str, "%Y-%m-%d").timestamp()
                    self._conn.execute("""
                        INSERT INTO samples_daily
                            (ts, date_str, pv_avg, pv_max, load_avg, load_max, grid_avg,
                             soc_avg, soc_min, soc_max,
                             pv_kwh, load_kwh, import_kwh, export_kwh,
                             bat_charge_kwh, bat_discharge_kwh,
                             self_sufficiency, samples)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(date_str) DO UPDATE SET
                            ts=excluded.ts, pv_avg=excluded.pv_avg, pv_max=excluded.pv_max,
                            load_avg=excluded.load_avg, load_max=excluded.load_max,
                            grid_avg=excluded.grid_avg,
                            soc_avg=excluded.soc_avg, soc_min=excluded.soc_min, soc_max=excluded.soc_max,
                            pv_kwh=excluded.pv_kwh, load_kwh=excluded.load_kwh,
                            import_kwh=excluded.import_kwh, export_kwh=excluded.export_kwh,
                            bat_charge_kwh=excluded.bat_charge_kwh, bat_discharge_kwh=excluded.bat_discharge_kwh,
                            self_sufficiency=excluded.self_sufficiency, samples=excluded.samples
                    """, (day_ts, date_str, row[0] or 0, row[1] or 0, row[2] or 0, row[3] or 0,
                          row[4] or 0, row[5] or 0, row[6] or 0, row[7] or 0,
                          round(row[8] or 0, 3), round(row[9] or 0, 3),
                          round(row[10] or 0, 3), round(row[11] or 0, 3),
                          round(row[12] or 0, 3), round(row[13] or 0, 3),
                          self_suff, row[14]))
                    report["daily_recomputed"] += 1

            self._conn.commit()
            logger.info(f"Recomputed {report['daily_recomputed']} daily aggregates")

        return {"status": "ok", "dry_run": dry_run, **report}

    def backfill_historical_export(self, dry_run: bool = False):
        """
        Estime rétroactivement export_kwh pour les jours passés où il vaut 0
        mais où pv_kwh > load_kwh (évidence qu'il y a eu export).

        Équation de conservation sur la journée :
          PV + Discharge + Import = Load + Charge + Export
        → Export = max(0, PV - Load + Import + Discharge - Charge)

        Cette estimation reste imparfaite (les charge/discharge antérieurs au fix
        ont aussi le même bug) mais redonne des valeurs cohérentes au lieu de 0.
        On n'applique le backfill QUE sur les jours où export_kwh==0 pour ne pas
        écraser des valeurs correctes (après le fix).
        """
        if not self._conn:
            return {"status": "error", "message": "DB not initialized"}

        rows = self._conn.execute("""
            SELECT ts, date_str, pv_kwh, load_kwh, import_kwh, export_kwh,
                   bat_charge_kwh, bat_discharge_kwh
            FROM samples_daily
            WHERE export_kwh = 0 AND pv_kwh > load_kwh
            ORDER BY ts
        """).fetchall()

        fixed = []
        for r in rows:
            ts, date_str, pv, load, imp, exp, bch, bdis = r
            # Estimation conservatrice
            est = max(0.0, (pv or 0) - (load or 0) + (imp or 0)
                       + (bdis or 0) - (bch or 0))
            est = round(est, 2)
            fixed.append({
                "date": date_str, "pv_kwh": pv, "load_kwh": load,
                "old_export": exp, "estimated_export": est,
            })
            if not dry_run and est > 0:
                self._conn.execute(
                    "UPDATE samples_daily SET export_kwh=? WHERE date_str=?",
                    (est, date_str)
                )

        if not dry_run:
            self._conn.commit()
            logger.info("Backfill export terminé : %d jours mis à jour", len(fixed))

        total_estimated = round(sum(f["estimated_export"] for f in fixed), 2)
        return {
            "status": "ok",
            "dry_run": dry_run,
            "days_fixed": len(fixed),
            "total_estimated_export_kwh": total_estimated,
            "sample": fixed[:10],
        }

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ═══════════════════════════════════════════════════════════════════════
    #  Écriture
    # ═══════════════════════════════════════════════════════════════════════

    def insert_sample(self, pv_power: float = 0, load_power: float = 0,
                      grid_power: float = 0, bat_power: float = 0,
                      bat_soc: float = 0, bat_voltage: float = 0,
                      frequency: float = 0, temperature: float = 0):
        """Insère un échantillon brut et déclenche les agrégats si besoin."""
        now = time.time()

        self._conn.execute(
            "INSERT INTO samples_raw (ts, pv_power, load_power, grid_power, "
            "bat_power, bat_soc, bat_voltage, frequency, temperature) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, pv_power, load_power, grid_power, bat_power,
             bat_soc, bat_voltage, frequency, temperature)
        )

        # Agrégation 5 min
        if now - self._last_5min_ts >= 300:
            self._aggregate_5min(now)
            self._last_5min_ts = now

        # Agrégation horaire
        if now - self._last_hourly_ts >= 3600:
            self._aggregate_hourly(now)
            self._last_hourly_ts = now

        # Agrégation journalière (une fois par heure max)
        if now - self._last_daily_ts >= 3600:
            self._aggregate_daily(now)
            self._last_daily_ts = now

        # Nettoyage périodique (toutes les 15 min)
        if now - self._last_cleanup_ts >= 900:
            self._cleanup(now)
            self._last_cleanup_ts = now

        self._conn.commit()

    def _aggregate_5min(self, now: float):
        """Crée un agrégat 5 min à partir des échantillons bruts des 5 dernières min.

        IMPORTANT : import_kwh / export_kwh sont calculés en sommant les VALEURS
        INSTANTANÉES positives/négatives séparément (et non via la moyenne),
        pour capturer correctement les alternances import⇄export dans la fenêtre.
        """
        t_start = now - 300
        row = self._conn.execute("""
            SELECT
                AVG(pv_power), MAX(pv_power),
                AVG(load_power), MAX(load_power),
                AVG(grid_power), MIN(grid_power), MAX(grid_power),
                AVG(bat_power), MIN(bat_power), MAX(bat_power),
                AVG(bat_soc), MIN(bat_soc), MAX(bat_soc),
                COUNT(*),
                AVG(CASE WHEN grid_power > 0 THEN grid_power ELSE 0 END),
                AVG(CASE WHEN grid_power < 0 THEN -grid_power ELSE 0 END),
                AVG(CASE WHEN bat_power  > 0 THEN bat_power  ELSE 0 END),
                AVG(CASE WHEN bat_power  < 0 THEN -bat_power ELSE 0 END),
                AVG(pv_power),
                AVG(load_power)
            FROM samples_raw WHERE ts > ? AND ts <= ?
        """, (t_start, now)).fetchone()

        if row and row[13] and row[13] > 0:
            # Énergie sur la fenêtre de 5 min = P_moyenne [W] * (5/60)h / 1000 = kWh
            H = 5.0 / 60.0 / 1000.0  # facteur puissance_moyenne → kWh sur 5 min
            pv_kwh             = round((row[18] or 0) * H, 4)
            load_kwh           = round((row[19] or 0) * H, 4)
            import_kwh         = round((row[14] or 0) * H, 4)
            export_kwh         = round((row[15] or 0) * H, 4)
            bat_charge_kwh     = round((row[16] or 0) * H, 4)
            bat_discharge_kwh  = round((row[17] or 0) * H, 4)

            # row[0:13] = 13 colonnes de données agrégées (pv_avg ... soc_max)
            # row[13]   = COUNT(*) = nombre de samples
            # row[14:]  = colonnes CASE WHEN et AVG supplémentaires (déjà extraites ci-dessus)
            # ON CONFLICT(ts) DO UPDATE → idempotent : si on rejoue l'agrégation pour
            # le même ts (recompute, restart), on écrase au lieu de dupliquer.
            self._conn.execute(
                "INSERT INTO samples_5min (ts, pv_avg, pv_max, load_avg, load_max, "
                "grid_avg, grid_min, grid_max, bat_avg, bat_min, bat_max, "
                "soc_avg, soc_min, soc_max, "
                "pv_kwh, load_kwh, import_kwh, export_kwh, "
                "bat_charge_kwh, bat_discharge_kwh, samples) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ts) DO UPDATE SET "
                "pv_avg=excluded.pv_avg, pv_max=excluded.pv_max, "
                "load_avg=excluded.load_avg, load_max=excluded.load_max, "
                "grid_avg=excluded.grid_avg, grid_min=excluded.grid_min, grid_max=excluded.grid_max, "
                "bat_avg=excluded.bat_avg, bat_min=excluded.bat_min, bat_max=excluded.bat_max, "
                "soc_avg=excluded.soc_avg, soc_min=excluded.soc_min, soc_max=excluded.soc_max, "
                "pv_kwh=excluded.pv_kwh, load_kwh=excluded.load_kwh, "
                "import_kwh=excluded.import_kwh, export_kwh=excluded.export_kwh, "
                "bat_charge_kwh=excluded.bat_charge_kwh, bat_discharge_kwh=excluded.bat_discharge_kwh, "
                "samples=excluded.samples",
                (now, *row[:13],
                 pv_kwh, load_kwh, import_kwh, export_kwh,
                 bat_charge_kwh, bat_discharge_kwh,
                 row[13])
            )

    def _aggregate_hourly(self, now: float):
        """Crée un agrégat horaire à partir des 5 min de la dernière heure.

        Les kWh sont SOMMÉS depuis samples_5min (calculés correctement là-bas),
        pas recalculés à partir de la moyenne horaire (ce qui ferait perdre
        les alternances import⇄export).
        """
        t_start = now - 3600
        row = self._conn.execute("""
            SELECT
                AVG(pv_avg), MAX(pv_max),
                AVG(load_avg), MAX(load_max),
                AVG(grid_avg), MIN(grid_min), MAX(grid_max),
                AVG(bat_avg), MIN(bat_min), MAX(bat_max),
                AVG(soc_avg), MIN(soc_min), MAX(soc_max),
                SUM(samples),
                SUM(pv_kwh), SUM(load_kwh),
                SUM(import_kwh), SUM(export_kwh),
                SUM(bat_charge_kwh), SUM(bat_discharge_kwh)
            FROM samples_5min WHERE ts > ? AND ts <= ?
        """, (t_start, now)).fetchone()

        if row and row[13] and row[13] > 0:
            pv_kwh            = round(row[14] or 0, 4)
            load_kwh          = round(row[15] or 0, 4)
            import_kwh        = round(row[16] or 0, 4)
            export_kwh        = round(row[17] or 0, 4)
            bat_charge_kwh    = round(row[18] or 0, 4)
            bat_discharge_kwh = round(row[19] or 0, 4)

            self._conn.execute(
                "INSERT INTO samples_hourly (ts, pv_avg, pv_max, load_avg, load_max, "
                "grid_avg, grid_min, grid_max, bat_avg, bat_min, bat_max, "
                "soc_avg, soc_min, soc_max, "
                "pv_kwh, load_kwh, import_kwh, export_kwh, "
                "bat_charge_kwh, bat_discharge_kwh, samples) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ts) DO UPDATE SET "
                "pv_avg=excluded.pv_avg, pv_max=excluded.pv_max, "
                "load_avg=excluded.load_avg, load_max=excluded.load_max, "
                "grid_avg=excluded.grid_avg, grid_min=excluded.grid_min, grid_max=excluded.grid_max, "
                "bat_avg=excluded.bat_avg, bat_min=excluded.bat_min, bat_max=excluded.bat_max, "
                "soc_avg=excluded.soc_avg, soc_min=excluded.soc_min, soc_max=excluded.soc_max, "
                "pv_kwh=excluded.pv_kwh, load_kwh=excluded.load_kwh, "
                "import_kwh=excluded.import_kwh, export_kwh=excluded.export_kwh, "
                "bat_charge_kwh=excluded.bat_charge_kwh, bat_discharge_kwh=excluded.bat_discharge_kwh, "
                "samples=excluded.samples",
                (now, *row[:13], pv_kwh, load_kwh, import_kwh, export_kwh,
                 bat_charge_kwh, bat_discharge_kwh, row[13])
            )

    def _aggregate_daily(self, now: float):
        """Crée / met à jour l'agrégat journalier."""
        import datetime
        today = datetime.date.today().isoformat()
        t_start_of_day = time.mktime(datetime.date.today().timetuple())

        row = self._conn.execute("""
            SELECT
                AVG(pv_avg), MAX(pv_max),
                AVG(load_avg), MAX(load_max),
                AVG(grid_avg),
                AVG(soc_avg), MIN(soc_min), MAX(soc_max),
                SUM(pv_kwh), SUM(load_kwh),
                SUM(import_kwh), SUM(export_kwh),
                SUM(bat_charge_kwh), SUM(bat_discharge_kwh),
                SUM(samples)
            FROM samples_hourly WHERE ts >= ?
        """, (t_start_of_day,)).fetchone()

        if row and row[14] and row[14] > 0:
            load_kwh = row[9] or 0
            import_kwh = row[10] or 0
            self_suff = round(((load_kwh - import_kwh) / load_kwh * 100)
                              if load_kwh > 0 else 0, 1)

            # UPSERT
            self._conn.execute("""
                INSERT INTO samples_daily
                    (ts, date_str, pv_avg, pv_max, load_avg, load_max, grid_avg,
                     soc_avg, soc_min, soc_max,
                     pv_kwh, load_kwh, import_kwh, export_kwh,
                     bat_charge_kwh, bat_discharge_kwh,
                     self_sufficiency, samples)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date_str) DO UPDATE SET
                    ts=excluded.ts, pv_avg=excluded.pv_avg, pv_max=excluded.pv_max,
                    load_avg=excluded.load_avg, load_max=excluded.load_max,
                    grid_avg=excluded.grid_avg,
                    soc_avg=excluded.soc_avg, soc_min=excluded.soc_min, soc_max=excluded.soc_max,
                    pv_kwh=excluded.pv_kwh, load_kwh=excluded.load_kwh,
                    import_kwh=excluded.import_kwh, export_kwh=excluded.export_kwh,
                    bat_charge_kwh=excluded.bat_charge_kwh,
                    bat_discharge_kwh=excluded.bat_discharge_kwh,
                    self_sufficiency=excluded.self_sufficiency, samples=excluded.samples
            """, (time.time(), today, *row[:14], self_suff, row[14]))

    def _cleanup(self, now: float):
        """Supprime les données au-delà de la rétention."""
        n1 = self._conn.execute(
            "DELETE FROM samples_raw WHERE ts < ?", (now - RETENTION_RAW,)
        ).rowcount
        n2 = self._conn.execute(
            "DELETE FROM samples_5min WHERE ts < ?", (now - RETENTION_5MIN,)
        ).rowcount
        n3 = self._conn.execute(
            "DELETE FROM samples_hourly WHERE ts < ?", (now - RETENTION_HOURLY,)
        ).rowcount
        if n1 or n2 or n3:
            logger.debug("Cleanup DB: raw=%d 5min=%d hourly=%d", n1, n2, n3)

    # ═══════════════════════════════════════════════════════════════════════
    #  Sprint 11 : Persistance Solax (tables séparées en parallèle de Victron)
    # ═══════════════════════════════════════════════════════════════════════

    def insert_solax_sample(self, pv_power: float = 0, load_power: float = 0,
                            grid_power: float = 0, bat_power: float = 0,
                            bat_soc: float = 0, bat_voltage: float = 0,
                            frequency: float = 0, temperature: float = 0):
        """Insère un échantillon Solax brut + déclenche les agrégats Solax."""
        now = time.time()

        self._conn.execute(
            "INSERT INTO solax_raw (ts, pv_power, load_power, grid_power, "
            "bat_power, bat_soc, bat_voltage, frequency, temperature) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, pv_power, load_power, grid_power, bat_power,
             bat_soc, bat_voltage, frequency, temperature)
        )

        # Agrégations Solax (cycles séparés de ceux Victron)
        if now - self._last_solax_5min_ts >= 300:
            self._aggregate_solax_5min(now)
            self._last_solax_5min_ts = now
        if now - self._last_solax_hourly_ts >= 3600:
            self._aggregate_solax_hourly(now)
            self._last_solax_hourly_ts = now
        if now - self._last_solax_daily_ts >= 3600:
            self._aggregate_solax_daily(now)
            self._last_solax_daily_ts = now
        if now - self._last_solax_cleanup_ts >= 900:
            self._cleanup_solax(now)
            self._last_solax_cleanup_ts = now

        self._conn.commit()

    def _aggregate_solax_5min(self, now: float):
        """Agrégat 5 min Solax depuis solax_raw."""
        t_start = now - 300
        row = self._conn.execute("""
            SELECT
                AVG(pv_power), MAX(pv_power),
                AVG(load_power), MAX(load_power),
                AVG(grid_power), MIN(grid_power), MAX(grid_power),
                AVG(bat_power), MIN(bat_power), MAX(bat_power),
                AVG(bat_soc), MIN(bat_soc), MAX(bat_soc),
                COUNT(*),
                AVG(CASE WHEN grid_power > 0 THEN grid_power ELSE 0 END),
                AVG(CASE WHEN grid_power < 0 THEN -grid_power ELSE 0 END),
                AVG(CASE WHEN bat_power  > 0 THEN bat_power  ELSE 0 END),
                AVG(CASE WHEN bat_power  < 0 THEN -bat_power ELSE 0 END),
                AVG(pv_power),
                AVG(load_power)
            FROM solax_raw WHERE ts > ? AND ts <= ?
        """, (t_start, now)).fetchone()

        if row and row[13] and row[13] > 0:
            H = 5.0 / 60.0 / 1000.0  # facteur P_moy → kWh sur 5 min
            self._conn.execute(
                "INSERT INTO solax_5min (ts, pv_avg, pv_max, load_avg, load_max, "
                "grid_avg, grid_min, grid_max, bat_avg, bat_min, bat_max, "
                "soc_avg, soc_min, soc_max, "
                "pv_kwh, load_kwh, import_kwh, export_kwh, "
                "bat_charge_kwh, bat_discharge_kwh, samples) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ts) DO UPDATE SET "
                "pv_avg=excluded.pv_avg, pv_max=excluded.pv_max, "
                "load_avg=excluded.load_avg, load_max=excluded.load_max, "
                "grid_avg=excluded.grid_avg, grid_min=excluded.grid_min, grid_max=excluded.grid_max, "
                "bat_avg=excluded.bat_avg, bat_min=excluded.bat_min, bat_max=excluded.bat_max, "
                "soc_avg=excluded.soc_avg, soc_min=excluded.soc_min, soc_max=excluded.soc_max, "
                "pv_kwh=excluded.pv_kwh, load_kwh=excluded.load_kwh, "
                "import_kwh=excluded.import_kwh, export_kwh=excluded.export_kwh, "
                "bat_charge_kwh=excluded.bat_charge_kwh, bat_discharge_kwh=excluded.bat_discharge_kwh, "
                "samples=excluded.samples",
                (now, *row[:13],
                 round((row[18] or 0) * H, 4),
                 round((row[19] or 0) * H, 4),
                 round((row[14] or 0) * H, 4),
                 round((row[15] or 0) * H, 4),
                 round((row[16] or 0) * H, 4),
                 round((row[17] or 0) * H, 4),
                 row[13])
            )

    def _aggregate_solax_hourly(self, now: float):
        """Agrégat horaire Solax depuis solax_5min."""
        t_start = now - 3600
        row = self._conn.execute("""
            SELECT
                AVG(pv_avg), MAX(pv_max),
                AVG(load_avg), MAX(load_max),
                AVG(grid_avg), MIN(grid_min), MAX(grid_max),
                AVG(bat_avg), MIN(bat_min), MAX(bat_max),
                AVG(soc_avg), MIN(soc_min), MAX(soc_max),
                SUM(samples),
                SUM(pv_kwh), SUM(load_kwh),
                SUM(import_kwh), SUM(export_kwh),
                SUM(bat_charge_kwh), SUM(bat_discharge_kwh)
            FROM solax_5min WHERE ts > ? AND ts <= ?
        """, (t_start, now)).fetchone()

        if row and row[13] and row[13] > 0:
            self._conn.execute(
                "INSERT INTO solax_hourly (ts, pv_avg, pv_max, load_avg, load_max, "
                "grid_avg, grid_min, grid_max, bat_avg, bat_min, bat_max, "
                "soc_avg, soc_min, soc_max, "
                "pv_kwh, load_kwh, import_kwh, export_kwh, "
                "bat_charge_kwh, bat_discharge_kwh, samples) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ts) DO UPDATE SET "
                "pv_avg=excluded.pv_avg, pv_max=excluded.pv_max, "
                "load_avg=excluded.load_avg, load_max=excluded.load_max, "
                "grid_avg=excluded.grid_avg, grid_min=excluded.grid_min, grid_max=excluded.grid_max, "
                "bat_avg=excluded.bat_avg, bat_min=excluded.bat_min, bat_max=excluded.bat_max, "
                "soc_avg=excluded.soc_avg, soc_min=excluded.soc_min, soc_max=excluded.soc_max, "
                "pv_kwh=excluded.pv_kwh, load_kwh=excluded.load_kwh, "
                "import_kwh=excluded.import_kwh, export_kwh=excluded.export_kwh, "
                "bat_charge_kwh=excluded.bat_charge_kwh, bat_discharge_kwh=excluded.bat_discharge_kwh, "
                "samples=excluded.samples",
                (now, *row[:13],
                 round(row[14] or 0, 4), round(row[15] or 0, 4),
                 round(row[16] or 0, 4), round(row[17] or 0, 4),
                 round(row[18] or 0, 4), round(row[19] or 0, 4),
                 row[13])
            )

    def _aggregate_solax_daily(self, now: float):
        """Agrégat journalier Solax depuis solax_hourly."""
        date_str = time.strftime("%Y-%m-%d", time.localtime(now))
        day_start = time.mktime(time.strptime(date_str, "%Y-%m-%d"))
        day_end = day_start + 86400
        row = self._conn.execute("""
            SELECT
                AVG(pv_avg), MAX(pv_max),
                AVG(load_avg), MAX(load_max),
                AVG(grid_avg),
                AVG(soc_avg), MIN(soc_min), MAX(soc_max),
                SUM(pv_kwh), SUM(load_kwh),
                SUM(import_kwh), SUM(export_kwh),
                SUM(bat_charge_kwh), SUM(bat_discharge_kwh),
                SUM(samples)
            FROM solax_hourly WHERE ts >= ? AND ts < ?
        """, (day_start, day_end)).fetchone()

        if row and row[14] and row[14] > 0:
            load_kwh = row[9] or 0
            import_kwh = row[10] or 0
            ss = round(((load_kwh - import_kwh) / load_kwh * 100) if load_kwh > 0 else 0, 1)
            self._conn.execute(
                "INSERT INTO solax_daily (ts, date_str, pv_avg, pv_max, load_avg, load_max, "
                "grid_avg, soc_avg, soc_min, soc_max, "
                "pv_kwh, load_kwh, import_kwh, export_kwh, "
                "bat_charge_kwh, bat_discharge_kwh, self_sufficiency, samples) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(date_str) DO UPDATE SET "
                "ts=excluded.ts, pv_avg=excluded.pv_avg, pv_max=excluded.pv_max, "
                "load_avg=excluded.load_avg, load_max=excluded.load_max, "
                "grid_avg=excluded.grid_avg, "
                "soc_avg=excluded.soc_avg, soc_min=excluded.soc_min, soc_max=excluded.soc_max, "
                "pv_kwh=excluded.pv_kwh, load_kwh=excluded.load_kwh, "
                "import_kwh=excluded.import_kwh, export_kwh=excluded.export_kwh, "
                "bat_charge_kwh=excluded.bat_charge_kwh, bat_discharge_kwh=excluded.bat_discharge_kwh, "
                "self_sufficiency=excluded.self_sufficiency, samples=excluded.samples",
                (day_start, date_str,
                 row[0] or 0, row[1] or 0, row[2] or 0, row[3] or 0,
                 row[4] or 0, row[5] or 0, row[6] or 0, row[7] or 0,
                 round(row[8] or 0, 3), round(load_kwh, 3),
                 round(import_kwh, 3), round(row[11] or 0, 3),
                 round(row[12] or 0, 3), round(row[13] or 0, 3), ss, row[14])
            )

    def _cleanup_solax(self, now: float):
        """Nettoyage des données Solax au-delà de la rétention."""
        n1 = self._conn.execute(
            "DELETE FROM solax_raw WHERE ts < ?", (now - RETENTION_RAW,)
        ).rowcount
        n2 = self._conn.execute(
            "DELETE FROM solax_5min WHERE ts < ?", (now - RETENTION_5MIN,)
        ).rowcount
        n3 = self._conn.execute(
            "DELETE FROM solax_hourly WHERE ts < ?", (now - RETENTION_HOURLY,)
        ).rowcount
        if n1 or n2 or n3:
            logger.debug("Cleanup Solax: raw=%d 5min=%d hourly=%d", n1, n2, n3)

    def get_solax_realtime(self, hours: int = 24) -> list:
        """Échantillons 5 min Solax sur les N dernières heures (graphique 24h)."""
        t_start = time.time() - hours * 3600
        rows = self._conn.execute(
            "SELECT ts, pv_avg, load_avg, grid_avg, bat_avg, soc_avg "
            "FROM solax_5min WHERE ts > ? ORDER BY ts", (t_start,)
        ).fetchall()
        return [{"ts": r[0], "pv": r[1], "load": r[2], "grid": r[3],
                 "bat": r[4], "soc": r[5]} for r in rows]

    def get_solax_hourly(self, days: int = 30) -> list:
        """Agrégats horaires Solax sur les N derniers jours."""
        t_start = time.time() - days * 86400
        rows = self._conn.execute(
            "SELECT ts, pv_avg, load_avg, grid_avg, bat_avg, soc_avg, "
            "pv_kwh, load_kwh, import_kwh, export_kwh "
            "FROM solax_hourly WHERE ts > ? ORDER BY ts", (t_start,)
        ).fetchall()
        return [{"ts": r[0], "pv": r[1], "load": r[2], "grid": r[3],
                 "bat": r[4], "soc": r[5], "pv_kwh": r[6], "load_kwh": r[7],
                 "import_kwh": r[8], "export_kwh": r[9]} for r in rows]

    def get_solax_daily(self, days: int = 365) -> list:
        """Agrégats journaliers Solax."""
        t_start = time.time() - days * 86400
        rows = self._conn.execute(
            "SELECT date_str, pv_kwh, load_kwh, import_kwh, export_kwh, "
            "bat_charge_kwh, bat_discharge_kwh, self_sufficiency, soc_min, soc_max "
            "FROM solax_daily WHERE ts > ? ORDER BY date_str", (t_start,)
        ).fetchall()
        return [{"date": r[0], "pv_kwh": r[1], "load_kwh": r[2],
                 "import_kwh": r[3], "export_kwh": r[4],
                 "bat_charge_kwh": r[5], "bat_discharge_kwh": r[6],
                 "self_sufficiency": r[7], "soc_min": r[8], "soc_max": r[9]}
                for r in rows]


    # ═══════════════════════════════════════════════════════════════════════
    #  Lecture (pour l'API)
    # ═══════════════════════════════════════════════════════════════════════

    def get_realtime(self, hours: int = 24) -> list:
        """Échantillons 5 min sur les N dernières heures."""
        t_start = time.time() - hours * 3600
        rows = self._conn.execute(
            "SELECT ts, pv_avg, load_avg, grid_avg, bat_avg, soc_avg "
            "FROM samples_5min WHERE ts > ? ORDER BY ts", (t_start,)
        ).fetchall()
        return [{"ts": r[0], "pv": r[1], "load": r[2], "grid": r[3],
                 "bat": r[4], "soc": r[5]} for r in rows]

    def get_hourly(self, days: int = 30) -> list:
        """Agrégats horaires sur les N derniers jours."""
        t_start = time.time() - days * 86400
        rows = self._conn.execute(
            "SELECT ts, pv_avg, pv_max, load_avg, load_max, grid_avg, "
            "soc_avg, soc_min, soc_max, "
            "pv_kwh, load_kwh, import_kwh, export_kwh, "
            "bat_charge_kwh, bat_discharge_kwh "
            "FROM samples_hourly WHERE ts > ? ORDER BY ts", (t_start,)
        ).fetchall()
        return [{"ts": r[0], "pv_avg": r[1], "pv_max": r[2],
                 "load_avg": r[3], "load_max": r[4], "grid_avg": r[5],
                 "soc_avg": r[6], "soc_min": r[7], "soc_max": r[8],
                 "pv_kwh": r[9], "load_kwh": r[10],
                 "import_kwh": r[11], "export_kwh": r[12],
                 "bat_charge_kwh": r[13], "bat_discharge_kwh": r[14]}
                for r in rows]

    def get_daily(self, days: int = 365) -> list:
        """Agrégats journaliers sur les N derniers jours."""
        t_start = time.time() - days * 86400
        rows = self._conn.execute(
            "SELECT ts, date_str, pv_avg, pv_max, load_avg, load_max, "
            "grid_avg, soc_avg, soc_min, soc_max, "
            "pv_kwh, load_kwh, import_kwh, export_kwh, "
            "bat_charge_kwh, bat_discharge_kwh, self_sufficiency "
            "FROM samples_daily WHERE ts > ? ORDER BY ts", (t_start,)
        ).fetchall()
        return [{"ts": r[0], "date": r[1], "pv_avg": r[2], "pv_max": r[3],
                 "load_avg": r[4], "load_max": r[5], "grid_avg": r[6],
                 "soc_avg": r[7], "soc_min": r[8], "soc_max": r[9],
                 "pv_kwh": r[10], "load_kwh": r[11],
                 "import_kwh": r[12], "export_kwh": r[13],
                 "bat_charge_kwh": r[14], "bat_discharge_kwh": r[15],
                 "self_sufficiency": r[16]}
                for r in rows]

    def get_stats_summary(self) -> dict:
        """Résumé rapide : taille DB, nombre d'enregistrements par table."""
        raw_count = self._conn.execute("SELECT COUNT(*) FROM samples_raw").fetchone()[0]
        min5_count = self._conn.execute("SELECT COUNT(*) FROM samples_5min").fetchone()[0]
        hourly_count = self._conn.execute("SELECT COUNT(*) FROM samples_hourly").fetchone()[0]
        daily_count = self._conn.execute("SELECT COUNT(*) FROM samples_daily").fetchone()[0]
        db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        return {
            "db_size_bytes": db_size,
            "db_size_mb": round(db_size / 1048576, 2),
            "raw_count": raw_count,
            "5min_count": min5_count,
            "hourly_count": hourly_count,
            "daily_count": daily_count,
        }

    # ═══ Sprint 6 : Forecast snapshots ═══

    def save_forecast_snapshot(self, daily_forecast: list):
        """
        Persiste une prévision quotidienne (5 jours à venir) pour comparaison ultérieure.
        `daily_forecast` : [{"date": "YYYY-MM-DD", "kwh": float}, ...]
        Idempotent : écrase la prévision du jour pour la même paire (snapshot_date, forecast_date).
        """
        if not self._conn or not daily_forecast:
            return 0
        import datetime as dt
        now = time.time()
        snap_date = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        count = 0
        for entry in daily_forecast:
            fdate = entry.get("date")
            fkwh = entry.get("kwh", 0) or 0
            if not fdate:
                continue
            try:
                # horizon = jours entre snapshot et forecast
                sd = dt.datetime.strptime(snap_date, "%Y-%m-%d")
                fd = dt.datetime.strptime(fdate, "%Y-%m-%d")
                horizon = (fd - sd).days
            except Exception:
                horizon = 0
            # REPLACE grâce à l'UNIQUE index
            self._conn.execute(
                "INSERT INTO solar_forecast_snapshots "
                "(snapshot_ts, snapshot_date, forecast_date, forecast_kwh, horizon_days) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(snapshot_date, forecast_date) DO UPDATE SET "
                "forecast_kwh=excluded.forecast_kwh, snapshot_ts=excluded.snapshot_ts",
                (now, snap_date, fdate, float(fkwh), horizon)
            )
            count += 1
        self._conn.commit()
        return count

    def get_forecast_vs_actual(self, days: int = 90):
        """
        Retourne l'historique prévision vs réel pour les N derniers jours.
        Joint les snapshots (pris la veille = horizon 1 de préférence, sinon horizon 0)
        avec les productions PV réelles mesurées dans samples_daily.
        """
        if not self._conn:
            return []
        t_start = time.time() - days * 86400

        # Pour chaque forecast_date, on prend la prévision faite la veille (horizon=1)
        # en priorité, sinon la plus récente disponible
        rows = self._conn.execute("""
            WITH best AS (
                SELECT forecast_date,
                       forecast_kwh,
                       horizon_days,
                       snapshot_date,
                       ROW_NUMBER() OVER (
                           PARTITION BY forecast_date
                           ORDER BY
                               CASE WHEN horizon_days = 1 THEN 0
                                    WHEN horizon_days = 0 THEN 1
                                    ELSE 2 END,
                               snapshot_ts DESC
                       ) AS rn
                FROM solar_forecast_snapshots
                WHERE snapshot_ts > ?
            )
            SELECT b.forecast_date, b.forecast_kwh, b.horizon_days, b.snapshot_date,
                   d.pv_kwh
            FROM best b
            LEFT JOIN samples_daily d ON d.date_str = b.forecast_date
            WHERE b.rn = 1
            ORDER BY b.forecast_date
        """, (t_start,)).fetchall()

        result = []
        for r in rows:
            fdate, fkwh, horizon, sdate, actual = r
            if actual is None:
                # Jour futur ou pas encore agrégé
                continue
            fkwh = float(fkwh or 0)
            actual = float(actual or 0)
            error = actual - fkwh
            error_pct = (error / fkwh * 100) if fkwh > 0.1 else None
            result.append({
                "date": fdate,
                "forecast_kwh": round(fkwh, 2),
                "actual_kwh": round(actual, 2),
                "error_kwh": round(error, 2),
                "error_pct": round(error_pct, 1) if error_pct is not None else None,
                "horizon_days": horizon,
                "snapshot_date": sdate,
            })
        return result

    def cleanup_old_forecasts(self, retention_days: int = 180):
        """Purge les snapshots plus anciens que N jours."""
        if not self._conn:
            return 0
        cutoff = time.time() - retention_days * 86400
        r = self._conn.execute(
            "DELETE FROM solar_forecast_snapshots WHERE snapshot_ts < ?", (cutoff,)
        )
        self._conn.commit()
        return r.rowcount
