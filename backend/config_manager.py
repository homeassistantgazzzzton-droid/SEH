"""
Smart Energy Hub — Configuration Manager
Gère un config.json persistant dans /data/.
Au démarrage, les variables d'environnement servent de valeurs par défaut.
Le config.json (s'il existe) les surcharge.
L'API web permet de modifier la config sans toucher au docker-compose.
Un changement de config notifie le main pour recharger les boucles de polling.
"""
import json
import logging
import os
from pathlib import Path
from copy import deepcopy

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config.json")

# ── Structure par défaut (sera remplie depuis les env vars au démarrage) ──
DEFAULT_CONFIG = {
    "inverter": {
        "type": "none",             # voltronic | victron | none
        "mode": "tcp",              # tcp | serial
        "host": "192.168.1.100",
        "port": 8899,
        "serial_port": "/dev/ttyUSB0",
        "baudrate": 2400,
        "ids": [1],
        "timeout": 5.0,
    },
    "victron": {
        "host": "192.168.1.50",
        "port": 502,
        "timeout": 5.0,
        "scan_ids": [],             # vide = scan large auto
        "invert_grid_sign": False,  # True si le Victron retourne grid > 0 quand export (compteur inversé)
        "invert_battery_sign": False,  # idem pour batterie (charge/décharge)
    },
    "bms_sources": [],              # Liste de sources BMS simultanées
    # Exemple :
    # [
    #   {"type":"pylontech","name":"Pylontech Stack","host":"192.168.1.101","port":9999,"num_batteries":5},
    #   {"type":"jkbms","name":"JK-BMS DIY","mode":"tcp","host":"192.168.1.102","port":502,"ids":[1,2],"timeout":3.0,"baudrate":115200,"serial_port":"/dev/ttyUSB1"}
    # ]
    "finance": {
        "enabled": False,
        "currency": "EUR",
        "contract_type": "fixed",
        "monthly_subscription": 0,
        "import_price": 0,
        "export_price": 0,
        "timezone": "Europe/Paris",
    },
    "general": {
        "poll_interval": 10,
        "data_dir": "/data",
    },
    "alerts": {
        "enabled": False,
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "soc_low_threshold": 20,
        "soc_critical_threshold": 10,
        "load_high_threshold": 85,
        "cooldown_minutes": 15,
        "daily_summary": True,
        "daily_summary_hour": 21,
        "weekly_summary": True,
        "weekly_summary_hour": 20,
        "pv_low_alert": True,
        "pv_low_threshold_pct": 40,
        "source_stale_alert": True,
        "source_stale_minutes": 15,
        "cycles_alert": True,
        "cycles_threshold": 4000,
    },
    "mqtt": {
        "enabled": False,
        "broker": "192.168.1.10",
        "port": 1883,
        "username": "",
        "password": "",
        "topic_prefix": "smart_energy_hub",
        "ha_discovery": True,
        "ha_discovery_prefix": "homeassistant",
        "publish_interval": 10,
    },
    "solar_forecast": {
        "enabled": False,
        "latitude": 43.32,
        "longitude": -0.37,
        "planes": [],
        "update_interval_minutes": 60,
        "loss_percent": 0,   # 0..50 — facteur de perte global (ombrages, salissure, etc.)
    },
    "roi": {
        "enabled": False,
        "installation_cost": 0,          # € — coût total (panneaux + onduleurs + batteries + pose)
        "commissioning_date": "",        # YYYY-MM-DD — date de mise en service
        "inflation_rate": 3.0,           # % / an — hausse tarif EDF
        "panel_degradation": 0.5,        # % / an — perte rendement panneaux
        "subscription_annual": 0,        # € / an — abonnement EDF annuel (pour calcul scénario sans solaire)
        "notes": "",
    },
    "leaf": {
        "enabled": False,
        "battery_capacity_kwh": 24,       # Nissan Leaf 24 kWh = génération ZE0
        "charge_power_kw": 3.6,           # Type 2 monophasé 16A sur OpenEVSE
        "target_soc_percent": 80,         # SoC cible après charge
        "current_soc_percent": 50,        # SoC actuel (à renseigner manuellement ou via API Leaf)
        "min_charge_kwh": 3,              # minimum de charge à assurer même en cas de météo pourrie
        "allow_grid_import": False,       # autorise complément grid si PV insuffisant
        "preferred_start_hour": 10,       # heure d'arrivée min (pour charge AC)
        "preferred_end_hour": 17,         # heure de départ max (à partir de laquelle on coupe)
    },
    "solax": {
        "enabled": False,
        "poll_interval_seconds": 10,
        "inverters": [
            # {
            #   "id": "main_solax",
            #   "plugin_name": "solax_x1_hybrid_gen4",
            #   "host": "192.168.1.50",
            #   "port": 502,
            #   "unit_id": 1,
            #   "timeout": 5,
            # },
        ],
    },
    "integrations": {
        "prometheus_enabled": True,       # active /metrics
        "influxdb_enabled": False,
        "influxdb_url": "",               # ex: http://192.168.1.10:8086
        "influxdb_org": "",
        "influxdb_bucket": "solar",
        "influxdb_token": "",
        "influxdb_push_interval_seconds": 30,
    },
    "support": {
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_use_tls": True,
        "smtp_username": "",
        "smtp_password": "",
        "from_email": "",
        "to_email": "",
    },
}


def _env(key: str, default=None):
    return os.environ.get(key, default)


def _env_int(key: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float = 0.0) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    return os.environ.get(key, str(default)).lower() in ("true", "1", "yes")


def _env_list_int(key: str, default=None) -> list:
    raw = os.environ.get(key, "")
    if not raw:
        return default or []
    try:
        return [int(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        return default or []


def build_config_from_env() -> dict:
    """Construit la config depuis les variables d'environnement (valeurs par défaut)."""
    cfg = deepcopy(DEFAULT_CONFIG)

    # Onduleur
    cfg["inverter"]["type"] = _env("INVERTER_TYPE", "none").lower()
    cfg["inverter"]["mode"] = _env("INVERTER_MODE", "tcp")
    cfg["inverter"]["host"] = _env("INVERTER_HOST", "192.168.1.100")
    cfg["inverter"]["port"] = _env_int("INVERTER_PORT", 8899)
    cfg["inverter"]["serial_port"] = _env("INVERTER_SERIAL", "/dev/ttyUSB0")
    cfg["inverter"]["baudrate"] = _env_int("INVERTER_BAUD", 2400)
    cfg["inverter"]["ids"] = _env_list_int("INVERTER_IDS", [1])
    cfg["inverter"]["timeout"] = _env_float("INVERTER_TIMEOUT", 5.0)

    # Victron
    cfg["victron"]["host"] = _env("VICTRON_HOST", _env("INVERTER_HOST", "192.168.1.50"))
    cfg["victron"]["port"] = _env_int("VICTRON_PORT", 502)
    cfg["victron"]["timeout"] = _env_float("VICTRON_TIMEOUT", 5.0)
    cfg["victron"]["scan_ids"] = _env_list_int("VICTRON_SCAN_IDS", [])

    # BMS — convertir l'ancien BMS_TYPE unique en liste de sources
    bms_type = _env("BMS_TYPE", "none").lower()
    bms_sources = []
    if bms_type == "pylontech" or _env("ELFIN_HOST"):
        if bms_type == "pylontech" or _env_int("NUM_BATTERIES", 0) > 0:
            bms_sources.append({
                "type": "pylontech",
                "name": "Pylontech",
                "host": _env("ELFIN_HOST", "192.168.1.100"),
                "port": _env_int("ELFIN_PORT", 9999),
                "num_batteries": _env_int("NUM_BATTERIES", 4),
                "enabled": bms_type == "pylontech",
            })
    if bms_type == "jkbms" or _env("JKBMS_HOST"):
        bms_sources.append({
            "type": "jkbms",
            "name": "JK-BMS",
            "mode": _env("JKBMS_MODE", "tcp"),
            "host": _env("JKBMS_HOST", "192.168.1.100"),
            "port": _env_int("JKBMS_PORT", 502),
            "ids": _env_list_int("JKBMS_IDS", [1]),
            "timeout": _env_float("JKBMS_TIMEOUT", 3.0),
            "baudrate": _env_int("JKBMS_BAUD", 115200),
            "serial_port": _env("JKBMS_SERIAL", "/dev/ttyUSB0"),
            "enabled": bms_type == "jkbms",
        })
    cfg["bms_sources"] = bms_sources

    # Finance
    cfg["finance"]["enabled"] = _env_bool("FINANCE_ENABLED", False)
    cfg["finance"]["currency"] = _env("FINANCE_CURRENCY", "EUR")
    cfg["finance"]["contract_type"] = _env("FINANCE_CONTRACT_TYPE", "fixed")
    cfg["finance"]["monthly_subscription"] = _env_float("FINANCE_MONTHLY_SUBSCRIPTION", 0)
    cfg["finance"]["import_price"] = _env_float("FINANCE_IMPORT_PRICE", 0)
    cfg["finance"]["export_price"] = _env_float("FINANCE_EXPORT_PRICE", 0)
    cfg["finance"]["timezone"] = _env("FINANCE_TIMEZONE", "Europe/Paris")

    # Général
    cfg["general"]["poll_interval"] = _env_int("POLL_INTERVAL", 10)
    cfg["general"]["data_dir"] = _env("DATA_DIR", "/data")

    return cfg


class ConfigManager:
    """Gestionnaire de configuration persistant."""

    def __init__(self, config_path: str = CONFIG_PATH):
        self.config_path = config_path
        self._config: dict = {}
        self._version: int = 0      # Incrémenté à chaque modification
        self._callbacks: list = []   # Fonctions appelées quand la config change

    def load(self):
        """Charge la config : config.json si existe, sinon env vars."""
        env_cfg = build_config_from_env()

        # Charger le config.json s'il existe
        p = Path(self.config_path)
        if p.exists():
            try:
                with open(p, "r") as f:
                    saved = json.load(f)
                # Fusionner : le fichier surcharge les valeurs par défaut
                self._config = self._deep_merge(env_cfg, saved)
                logger.info("Config chargée depuis %s (v%d)", self.config_path, self._version)
            except Exception as e:
                logger.warning("Erreur lecture config.json: %s — utilisation des env vars", e)
                self._config = env_cfg
        else:
            self._config = env_cfg
            # Sauvegarder la config initiale
            self.save()
            logger.info("Config initiale créée depuis env vars → %s", self.config_path)

    def save(self):
        """Sauvegarde la config dans config.json."""
        try:
            Path(self.config_path).parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w") as f:
                json.dump(self._config, f, indent=2, ensure_ascii=False)
            logger.info("Config sauvegardée → %s", self.config_path)
        except Exception as e:
            logger.error("Erreur sauvegarde config: %s", e)

    def get(self) -> dict:
        """Retourne une copie de la config."""
        return deepcopy(self._config)

    def update(self, new_config: dict) -> dict:
        """Met à jour la config, sauvegarde, et notifie les callbacks."""
        self._config = self._deep_merge(self._config, new_config)
        self._version += 1
        self.save()
        # Notifier les callbacks
        for cb in self._callbacks:
            try:
                cb(self._config)
            except Exception as e:
                logger.error("Config callback error: %s", e)
        return self.get()

    def on_change(self, callback):
        """Enregistre un callback appelé quand la config change."""
        self._callbacks.append(callback)

    @property
    def version(self) -> int:
        return self._version

    # ── Accesseurs rapides ──

    @property
    def inverter_type(self) -> str:
        return self._config.get("inverter", {}).get("type", "none")

    @property
    def bms_sources(self) -> list:
        return self._config.get("bms_sources", [])

    @property
    def enabled_bms_sources(self) -> list:
        return [s for s in self.bms_sources if s.get("enabled", True)]

    @property
    def poll_interval(self) -> int:
        return self._config.get("general", {}).get("poll_interval", 10)

    @property
    def finance_enabled(self) -> bool:
        return self._config.get("finance", {}).get("enabled", False)

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        """Fusion récursive : override surcharge base."""
        result = deepcopy(base)
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = ConfigManager._deep_merge(result[k], v)
            else:
                result[k] = deepcopy(v)
        return result
