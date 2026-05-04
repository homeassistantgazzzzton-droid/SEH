"""
Smart Energy Finance Engine (standalone Python)
Portage du moteur Node-RED Smart Energy Finance en Python pur.
Calcule les coûts, économies, autosuffisance jour/mois/année.

Fonctionne de manière 100% standalone, sans HA ni MQTT.
Les données d'énergie proviennent directement des onduleurs / BMS.
"""
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class EnergyAccumulator:
    solar: float = 0.0
    load: float = 0.0
    grid_import: float = 0.0
    grid_export: float = 0.0
    battery_charge: float = 0.0
    battery_discharge: float = 0.0


@dataclass
class FinanceAccumulator:
    import_cost: float = 0.0
    export_revenue: float = 0.0
    theoretical_no_solar_cost: float = 0.0
    solar_production_value: float = 0.0
    battery_discharge_value: float = 0.0
    home_supply_savings: float = 0.0


@dataclass
class TariffSlot:
    name: str = ""
    price: float = 0.0
    start: str = ""
    end: str = ""


@dataclass
class TempoConfig:
    color_entity: str = ""
    blue_hc: float = 0.0
    blue_hp: float = 0.0
    white_hc: float = 0.0
    white_hp: float = 0.0
    red_hc: float = 0.0
    red_hp: float = 0.0
    hc_slot1_start: str = "22:00"
    hc_slot1_end: str = "06:00"
    hc_slot2_start: str = ""
    hc_slot2_end: str = ""


@dataclass
class FinanceConfig:
    currency: str = "EUR"
    contract_type: str = "fixed"  # fixed | time_based | tempo
    monthly_subscription: float = 0.0
    fixed_import_price: float = 0.0
    fixed_export_price: float = 0.0
    tariffs: list = field(default_factory=lambda: [
        TariffSlot("peak", 0.0, "06:00", "22:00"),
        TariffSlot("off_peak", 0.0, "22:00", "06:00"),
    ])
    tempo: TempoConfig = field(default_factory=TempoConfig)
    timezone: str = "Europe/Paris"


@dataclass
class PeriodData:
    """Données d'une période (jour/mois/année)."""
    energy: dict = field(default_factory=dict)
    finance: dict = field(default_factory=dict)
    ratios: dict = field(default_factory=dict)


def _round(v: float, d: int = 2) -> float:
    return round(float(v or 0), d)


def _hhmm_to_minutes(s: str) -> Optional[int]:
    if not s or len(s) != 5 or s[2] != ":":
        return None
    try:
        h, m = int(s[:2]), int(s[3:])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except ValueError:
        pass
    return None


def _is_minute_in_range(minute: int, start: str, end: str) -> bool:
    s = _hhmm_to_minutes(start)
    e = _hhmm_to_minutes(end)
    if s is None or e is None:
        return False
    if s == e:
        return True
    if s < e:
        return s <= minute < e
    return minute >= s or minute < e


def _delta_positive(cur: float, prev: float) -> float:
    d = float(cur or 0) - float(prev or 0)
    return _round(d, 6) if d > 0 else 0


class EnergyFinanceEngine:
    """
    Moteur de calcul financier standalone.
    Accumule les données d'énergie et calcule les coûts/économies.
    """

    def __init__(self, config: FinanceConfig = None, data_dir: str = "/data"):
        self.config = config or FinanceConfig()
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self._store = None
        self._load_store()

    def _store_path(self) -> Path:
        return self.data_dir / "finance_store.json"

    def _load_store(self):
        """Charge le store persistant."""
        try:
            p = self._store_path()
            if p.exists():
                with open(p, "r") as f:
                    self._store = json.load(f)
                logger.info("Finance store chargé: %s", p)
                return
        except Exception as e:
            logger.warning("Erreur chargement store: %s", e)
        self._store = None

    def _save_store(self):
        """Sauvegarde le store."""
        try:
            with open(self._store_path(), "w") as f:
                json.dump(self._store, f, indent=2)
        except Exception as e:
            logger.error("Erreur sauvegarde store: %s", e)

    def _now_parts(self) -> dict:
        """Retourne les composantes date/heure dans le timezone configuré."""
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(self.config.timezone)
        now = datetime.now(tz)
        return {
            "year": f"{now.year:04d}",
            "month": f"{now.month:02d}",
            "day": f"{now.day:02d}",
            "hour": f"{now.hour:02d}",
            "minute": f"{now.minute:02d}",
            "second": f"{now.second:02d}",
            "day_key": now.strftime("%Y-%m-%d"),
            "month_key": now.strftime("%Y-%m"),
            "year_key": f"{now.year:04d}",
        }

    def _empty_energy(self) -> dict:
        return {"solar": 0, "load": 0, "grid_import": 0, "grid_export": 0,
                "battery_charge": 0, "battery_discharge": 0}

    def _empty_finance(self) -> dict:
        return {"import_cost": 0, "export_revenue": 0,
                "theoretical_no_solar_cost": 0, "solar_production_value": 0,
                "battery_discharge_value": 0, "home_supply_savings": 0}

    def _add_energy(self, a: dict, b: dict) -> dict:
        return {k: _round(float(a.get(k, 0)) + float(b.get(k, 0)), 6)
                for k in ["solar", "load", "grid_import", "grid_export",
                           "battery_charge", "battery_discharge"]}

    def _add_finance(self, a: dict, b: dict) -> dict:
        keys = ["import_cost", "export_revenue", "theoretical_no_solar_cost",
                "solar_production_value", "battery_discharge_value",
                "home_supply_savings"]
        return {k: _round(float(a.get(k, 0)) + float(b.get(k, 0)), 6)
                for k in keys}

    def _energy_delta(self, cur: dict, prev: dict) -> dict:
        return {
            "solar": _delta_positive(cur.get("solar", 0), prev.get("solar", 0)),
            "load": _delta_positive(cur.get("load", 0), prev.get("load", 0)),
            "grid_import": _delta_positive(cur.get("grid_import", 0), prev.get("grid_import", 0)),
            "grid_export": _delta_positive(cur.get("grid_export", 0), prev.get("grid_export", 0)),
            "battery_charge": _delta_positive(cur.get("battery_charge", 0), prev.get("battery_charge", 0)),
            "battery_discharge": _delta_positive(cur.get("battery_discharge", 0), prev.get("battery_discharge", 0)),
        }

    def _prorated_subscription(self, period: str, now: dict) -> float:
        p = float(self.config.monthly_subscription or 0)
        if not p:
            return 0
        if period == "day":
            import calendar
            days = calendar.monthrange(int(now["year"]), int(now["month"]))[1]
            return p / days
        if period == "month":
            return p
        if period == "year":
            return p * 12
        return 0

    def _get_current_tariff(self, now: dict) -> dict:
        """Détermine le tarif en cours."""
        minute = int(now["hour"]) * 60 + int(now["minute"])
        cfg = self.config

        if cfg.contract_type == "fixed":
            return {
                "mode": "fixed", "name": "fixed", "period": "fixed",
                "import_price": cfg.fixed_import_price,
                "export_price": cfg.fixed_export_price,
                "tempo_color": "", "is_tempo_hc": False,
            }

        if cfg.contract_type == "time_based":
            for t in cfg.tariffs:
                if t.name and t.start and t.end:
                    slot = t if isinstance(t, TariffSlot) else TariffSlot(**t)
                    if _is_minute_in_range(minute, slot.start, slot.end):
                        return {
                            "mode": "time_based", "name": slot.name,
                            "period": slot.name,
                            "import_price": slot.price,
                            "export_price": cfg.fixed_export_price,
                            "tempo_color": "", "is_tempo_hc": False,
                        }
            return {
                "mode": "time_based", "name": "unmatched", "period": "unmatched",
                "import_price": 0, "export_price": cfg.fixed_export_price,
                "tempo_color": "", "is_tempo_hc": False,
            }

        if cfg.contract_type == "tempo":
            tempo = cfg.tempo if isinstance(cfg.tempo, TempoConfig) else TempoConfig(**cfg.tempo)
            # La couleur tempo devra être fournie par un capteur externe
            color = self._store.get("meta", {}).get("last_tempo_color", "unknown") if self._store else "unknown"
            hc = _is_minute_in_range(minute, tempo.hc_slot1_start, tempo.hc_slot1_end)
            if tempo.hc_slot2_start and tempo.hc_slot2_end:
                hc = hc or _is_minute_in_range(minute, tempo.hc_slot2_start, tempo.hc_slot2_end)

            price_map = {
                "blue": (tempo.blue_hc, tempo.blue_hp),
                "white": (tempo.white_hc, tempo.white_hp),
                "red": (tempo.red_hc, tempo.red_hp),
            }
            prices = price_map.get(color, (0, 0))
            import_price = prices[0] if hc else prices[1]
            name = f"tempo_{color}_{'hc' if hc else 'hp'}"

            return {
                "mode": "tempo", "name": name,
                "period": "hc" if hc else "hp",
                "import_price": import_price,
                "export_price": cfg.fixed_export_price,
                "tempo_color": color, "is_tempo_hc": hc,
            }

        return {
            "mode": "fixed", "name": "fixed", "period": "fixed",
            "import_price": cfg.fixed_import_price,
            "export_price": cfg.fixed_export_price,
            "tempo_color": "", "is_tempo_hc": False,
        }

    def _build_period_finance(self, energy: dict, finance: dict,
                              period: str, now: dict) -> dict:
        """Construit les données financières d'une période."""
        imp_e = float(energy.get("grid_import", 0))
        exp_e = float(energy.get("grid_export", 0))
        load_e = float(energy.get("load", 0))
        solar_e = float(energy.get("solar", 0))
        bat_chg = float(energy.get("battery_charge", 0))
        bat_dis = float(energy.get("battery_discharge", 0))

        imp_cost = float(finance.get("import_cost", 0))
        exp_rev = float(finance.get("export_revenue", 0))
        theo = float(finance.get("theoretical_no_solar_cost", 0))
        solar_val = float(finance.get("solar_production_value", 0))
        bat_val = float(finance.get("battery_discharge_value", 0))
        home_sav = float(finance.get("home_supply_savings", 0))

        sub = self._prorated_subscription(period, now)
        real_total = imp_cost + sub - exp_rev
        savings = theo - imp_cost + exp_rev

        self_suff = ((max(0, load_e - imp_e)) / load_e * 100) if load_e > 0 else 0
        grid_dep = (imp_e / load_e * 100) if load_e > 0 else 0

        return {
            "energy": {
                "solar_kwh": _round(solar_e),
                "load_kwh": _round(load_e),
                "grid_import_kwh": _round(imp_e),
                "grid_export_kwh": _round(exp_e),
                "battery_charge_kwh": _round(bat_chg),
                "battery_discharge_kwh": _round(bat_dis),
            },
            "finance": {
                "import_cost": _round(imp_cost),
                "export_revenue": _round(exp_rev),
                "subscription_cost": _round(sub),
                "theoretical_no_solar_cost": _round(theo),
                "solar_production_value": _round(solar_val),
                "battery_discharge_value": _round(bat_val),
                "home_supply_savings": _round(home_sav),
                "real_total_with_subscription": _round(real_total),
                "savings_vs_no_solar": _round(savings),
            },
            "ratios": {
                "self_sufficiency_pct": _round(self_suff),
                "grid_dependency_pct": _round(grid_dep),
            },
        }

    def update(self, live_energy: dict) -> dict:
        """
        Met à jour le moteur avec les données d'énergie courantes.

        live_energy: {
            "solar": kWh aujourd'hui,
            "load": kWh aujourd'hui,
            "grid_import": kWh aujourd'hui,
            "grid_export": kWh aujourd'hui,
            "battery_charge": kWh aujourd'hui,
            "battery_discharge": kWh aujourd'hui,
        }

        Retourne le snapshot complet (jour/mois/année + historique).
        """
        now = self._now_parts()

        # Initialiser le store si nécessaire
        if not self._store:
            self._store = {
                "keys": {"day_key": now["day_key"], "month_key": now["month_key"],
                         "year_key": now["year_key"]},
                "prev_live_day": dict(live_energy),
                "closed_day_month_energy": self._empty_energy(),
                "closed_day_year_energy": self._empty_energy(),
                "current_day_finance": self._empty_finance(),
                "closed_day_month_finance": self._empty_finance(),
                "closed_day_year_finance": self._empty_finance(),
                "current_day_snapshot": dict(live_energy),
                "last_closed_day_key": "",
                "history": {"daily": [], "monthly": [], "yearly": [],
                            "last_archived_month_key": "",
                            "last_archived_year_key": ""},
                "meta": {"last_tempo_color": "", "last_tariff_name": "",
                         "last_run": ""},
            }

        store = self._store
        old_day_key = store["keys"]["day_key"]
        old_month_key = store["keys"]["month_key"]
        old_year_key = store["keys"]["year_key"]

        tariff = self._get_current_tariff(now)

        # ── Rotation de jour ─────────────────────────────────
        if old_day_key != now["day_key"]:
            if store.get("last_closed_day_key") != old_day_key:
                closed = self._build_period_finance(
                    store.get("current_day_snapshot", self._empty_energy()),
                    store.get("current_day_finance", self._empty_finance()),
                    "day",
                    {"year": old_day_key[:4], "month": old_day_key[5:7],
                     "day": old_day_key[8:10]},
                )
                store["history"]["daily"].append({"key": old_day_key, **closed})
                store["history"]["daily"] = store["history"]["daily"][-62:]

                store["closed_day_month_energy"] = self._add_energy(
                    store.get("closed_day_month_energy", self._empty_energy()),
                    store.get("current_day_snapshot", self._empty_energy()))
                store["closed_day_year_energy"] = self._add_energy(
                    store.get("closed_day_year_energy", self._empty_energy()),
                    store.get("current_day_snapshot", self._empty_energy()))
                store["closed_day_month_finance"] = self._add_finance(
                    store.get("closed_day_month_finance", self._empty_finance()),
                    store.get("current_day_finance", self._empty_finance()))
                store["closed_day_year_finance"] = self._add_finance(
                    store.get("closed_day_year_finance", self._empty_finance()),
                    store.get("current_day_finance", self._empty_finance()))
                store["last_closed_day_key"] = old_day_key

            # Rotation mois
            if old_month_key != now["month_key"]:
                if store["history"].get("last_archived_month_key") != old_month_key:
                    closed_m = self._build_period_finance(
                        store.get("closed_day_month_energy", self._empty_energy()),
                        store.get("closed_day_month_finance", self._empty_finance()),
                        "month",
                        {"year": old_month_key[:4], "month": old_month_key[5:7]},
                    )
                    store["history"]["monthly"].append({"key": old_month_key, **closed_m})
                    store["history"]["monthly"] = store["history"]["monthly"][-24:]
                    store["history"]["last_archived_month_key"] = old_month_key

                store["closed_day_month_energy"] = self._empty_energy()
                store["closed_day_month_finance"] = self._empty_finance()

            # Rotation année
            if old_year_key != now["year_key"]:
                if store["history"].get("last_archived_year_key") != old_year_key:
                    closed_y = self._build_period_finance(
                        store.get("closed_day_year_energy", self._empty_energy()),
                        store.get("closed_day_year_finance", self._empty_finance()),
                        "year", {"year": old_year_key},
                    )
                    store["history"]["yearly"].append({"key": old_year_key, **closed_y})
                    store["history"]["yearly"] = store["history"]["yearly"][-10:]
                    store["history"]["last_archived_year_key"] = old_year_key

                store["closed_day_year_energy"] = self._empty_energy()
                store["closed_day_year_finance"] = self._empty_finance()

            store["current_day_finance"] = self._empty_finance()
            store["prev_live_day"] = dict(live_energy)

        # ── Accumulation intra-jour ──────────────────────────
        if old_day_key == now["day_key"]:
            delta = self._energy_delta(
                live_energy,
                store.get("prev_live_day", self._empty_energy()))

            fin = store.get("current_day_finance", self._empty_finance())
            imp_price = float(tariff.get("import_price", 0))
            exp_price = float(tariff.get("export_price", 0))

            fin["import_cost"] = _round(
                fin["import_cost"] + delta["grid_import"] * imp_price, 6)
            fin["export_revenue"] = _round(
                fin["export_revenue"] + delta["grid_export"] * exp_price, 6)
            fin["theoretical_no_solar_cost"] = _round(
                fin["theoretical_no_solar_cost"] + delta["load"] * imp_price, 6)
            fin["solar_production_value"] = _round(
                fin["solar_production_value"] + delta["solar"] * imp_price, 6)
            fin["battery_discharge_value"] = _round(
                fin["battery_discharge_value"] + delta["battery_discharge"] * imp_price, 6)
            covered = max(0, float(delta["load"]) - float(delta["grid_import"]))
            fin["home_supply_savings"] = _round(
                fin["home_supply_savings"] + covered * imp_price, 6)

            store["current_day_finance"] = fin
            store["prev_live_day"] = dict(live_energy)

        # ── Update keys ──────────────────────────────────────
        store["keys"] = {"day_key": now["day_key"],
                         "month_key": now["month_key"],
                         "year_key": now["year_key"]}
        store["current_day_snapshot"] = dict(live_energy)
        store["meta"]["last_run"] = datetime.now(timezone.utc).isoformat()
        store["meta"]["last_tariff_name"] = tariff["name"]

        # Calcul des périodes courantes
        day_data = self._build_period_finance(
            live_energy, store.get("current_day_finance", self._empty_finance()),
            "day", now)
        month_energy = self._add_energy(
            store.get("closed_day_month_energy", self._empty_energy()), live_energy)
        month_finance = self._add_finance(
            store.get("closed_day_month_finance", self._empty_finance()),
            store.get("current_day_finance", self._empty_finance()))
        month_data = self._build_period_finance(month_energy, month_finance, "month", now)
        year_energy = self._add_energy(
            store.get("closed_day_year_energy", self._empty_energy()), live_energy)
        year_finance = self._add_finance(
            store.get("closed_day_year_finance", self._empty_finance()),
            store.get("current_day_finance", self._empty_finance()))
        year_data = self._build_period_finance(year_energy, year_finance, "year", now)

        # Sauvegarder
        self._save_store()

        return {
            "tariff": tariff,
            "day": day_data,
            "month": month_data,
            "year": year_data,
            "history": {
                "daily": store["history"]["daily"],
                "monthly": store["history"]["monthly"],
                "yearly": store["history"]["yearly"],
            },
            "config": {
                "currency": self.config.currency,
                "contract_type": self.config.contract_type,
                "timezone": self.config.timezone,
            },
            "updated_at": store["meta"]["last_run"],
        }

    def get_snapshot(self) -> Optional[dict]:
        """Retourne le dernier snapshot sans mise à jour."""
        if not self._store:
            return None
        now = self._now_parts()
        tariff = self._get_current_tariff(now)
        live = self._store.get("current_day_snapshot", self._empty_energy())
        day_data = self._build_period_finance(
            live, self._store.get("current_day_finance", self._empty_finance()),
            "day", now)
        return {
            "tariff": tariff,
            "day": day_data,
            "config": {
                "currency": self.config.currency,
                "contract_type": self.config.contract_type,
            },
            "updated_at": self._store.get("meta", {}).get("last_run", ""),
        }
