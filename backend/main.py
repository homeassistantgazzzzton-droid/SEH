"""
Smart Energy Hub — Backend unifié FastAPI v2
  INVERTER_TYPE = voltronic | victron | none
  BMS multi-sources : pylontech + jkbms simultanés, chacun dans son groupe
  Configuration persistante via /data/config.json, éditable depuis l'UI web
"""
import asyncio
import json
import logging
import os
import time
from typing import Optional

from pydantic import BaseModel
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

# Sprint 10 — Auth + onboarding
from auth import (
    init_auth, get_auth, get_current_user, get_current_admin,
    auth_router, User, COOKIE_NAME,
)
from setup import init_setup, setup_router
from network_scanner import NetworkScanner
from support import init_support, support_router

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
#  Globals
# ═══════════════════════════════════════════════════════════════════════════
_db = None
_cfg = None        # ConfigManager
_tasks = []        # polling tasks
_restart_event = asyncio.Event()
_alerts = None     # AlertManager
_mqtt = None       # MQTTPublisher
_forecast = None   # SolarForecast
_influx = None     # InfluxPublisher
_solax = None      # SolaxFleet (Sprint 8)

_cache: dict = {
    "inverter": {"units": {}, "last_update": None, "error": None, "type": "none"},
    "bms_groups": {},     # {"pylontech_0": {"name":"Pylontech Stack","type":"pylontech","units":{...}}, ...}
    "solarchargers": {"units": {}, "last_update": None, "error": None},
    "victron_system": {"data": None, "last_update": None, "error": None},
    "solax": {},  # {inv_id: {online, data, raw, last_update}, ...} (Sprint 8)
    "finance": {"data": None, "last_update": None, "error": None, "enabled": False},
    "system": {"uptime_start": time.time()},
}

_ws_clients: list[WebSocket] = []


# ═══════════════════════════════════════════════════════════════════════════
#  Sprint 13 : Polling adaptatif (ralentit si CPU surchargé)
# ═══════════════════════════════════════════════════════════════════════════

async def adaptive_sleep(base_seconds: float):
    """
    Variante de asyncio.sleep qui multiplie la durée par le throttle factor
    du PerfMonitor. Si CPU surchargé, le polling ralentit automatiquement.
    """
    try:
        from perf_monitor import get_monitor
        factor = get_monitor().get_throttle_factor()
    except Exception:
        factor = 1.0
    await asyncio.sleep(base_seconds * factor)


async def broadcast(data: dict):
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.remove(ws)


def _record_to_db():
    """Enregistre un échantillon dans la DB."""
    if not _db:
        return
    try:
        vicSystem = _cache["victron_system"].get("data")
        invUnits = _cache["inverter"].get("units", {})
        scUnits = _cache["solarchargers"].get("units", {})

        pv, load, grid, bat_p, soc, bat_v, freq, temp = 0, 0, 0, 0, 0, 0, 0, 0

        if vicSystem and vicSystem.get("online"):
            for sc in scUnits.values():
                if sc.get("online"):
                    pv += float(sc.get("pv_power", 0) or 0)
            pv += float(vicSystem.get("pv_on_output_power", 0) or 0)
            pv += float(vicSystem.get("pv_on_grid_power", 0) or 0)
            load = float(vicSystem.get("consumption_power", 0) or 0)
            grid = float(vicSystem.get("grid_power", 0) or 0)
            bat_p = float(vicSystem.get("battery_power", 0) or 0)
            soc = float(vicSystem.get("battery_soc", 0) or 0)
            bat_v = float(vicSystem.get("battery_voltage", 0) or 0)
        else:
            for inv in invUnits.values():
                if not inv.get("online"):
                    continue
                pv += float(inv.get("pv_input_power", 0) or 0)
                load += float(inv.get("output_active_power", 0) or 0)
                inv_soc = inv.get("battery_capacity")
                if inv_soc is not None:
                    soc = float(inv_soc)
                inv_bv = float(inv.get("battery_voltage", 0) or 0)
                if inv_bv:
                    bat_v = inv_bv
                chg = float(inv.get("battery_charge_current", 0) or 0)
                dis = float(inv.get("battery_discharge_current", 0) or 0)
                bat_p += (chg - dis) * inv_bv
                freq = float(inv.get("output_frequency", 0) or 0)
                temp = float(inv.get("inverter_temperature", 0) or 0)

        # BMS SoC — moyenne de tous les groupes BMS
        if soc == 0:
            all_socs = []
            for grp in _cache["bms_groups"].values():
                for b in grp.get("units", {}).values():
                    if b.get("online") and b.get("soc") is not None:
                        all_socs.append(float(b["soc"]))
            if all_socs:
                soc = sum(all_socs) / len(all_socs)

        # Note : l'inversion de signe grid/battery est appliquée dans la boucle
        # victron_loop au niveau du cache, donc `grid` et `bat_p` lus ici sont
        # déjà corrigés. Rien à refaire ici.

        _db.insert_sample(pv_power=pv, load_power=load, grid_power=grid,
                          bat_power=bat_p, bat_soc=soc, bat_voltage=bat_v,
                          frequency=freq, temperature=temp)
    except Exception as e:
        logger.debug("DB record error: %s", e)

    # Alertes Telegram
    if _alerts and _alerts.enabled:
        try:
            asyncio.ensure_future(_alerts.check_and_alert(_cache))
        except Exception as e:
            logger.debug("Alert check error: %s", e)

    # MQTT publish
    if _mqtt and _mqtt.enabled:
        try:
            _mqtt.publish_state(_cache)
        except Exception as e:
            logger.debug("MQTT publish error: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
#  Boucles de polling
# ═══════════════════════════════════════════════════════════════════════════

async def inverter_loop(cfg: dict):
    """Boucle Voltronic."""
    from voltronic import VoltronicFleet, VoltronicClient
    c = cfg["inverter"]
    # Sécuriser le type des ids
    raw_ids = c.get("ids", [1])
    if isinstance(raw_ids, int):
        inv_ids = [raw_ids]
    elif isinstance(raw_ids, str):
        inv_ids = [int(x.strip()) for x in raw_ids.split(",") if x.strip()]
    elif isinstance(raw_ids, list):
        inv_ids = [int(x) for x in raw_ids if x]
    else:
        inv_ids = [1]

    fleet = VoltronicFleet(
        mode=str(c.get("mode", "tcp")),
        host=str(c.get("host", "192.168.1.100")),
        port=int(c.get("port", 8899)),
        serial_port=str(c.get("serial_port", "/dev/ttyUSB0")),
        baudrate=int(c.get("baudrate", 2400)),
        inverter_ids=inv_ids,
        timeout=float(c.get("timeout", 5.0)),
        poll_interval=cfg["general"]["poll_interval"],
    )
    tmp = VoltronicClient()
    while True:
        try:
            results = await asyncio.wait_for(fleet.poll_all(), timeout=30.0)
            units = {str(s.inverter_id): tmp.to_dict(s) for s in results}
            _cache["inverter"]["units"] = units
            _cache["inverter"]["last_update"] = time.time()
            _cache["inverter"]["error"] = None
            _cache["inverter"]["type"] = "voltronic"
            await broadcast({"type": "inverter_update", "data": _cache["inverter"]})
            _record_to_db()
            try:
                from health_tracker import get_tracker
                get_tracker().record("inverter_ok")
            except Exception:
                pass
        except Exception as e:
            _cache["inverter"]["error"] = str(e)
            logger.error("Inverter: %s", e)
            try:
                from health_tracker import get_tracker
                get_tracker().record("inverter_fail", str(e))
            except Exception:
                pass
        await adaptive_sleep(cfg["general"]["poll_interval"])


async def victron_loop(cfg: dict):
    """Boucle Victron (un seul Cerbo GX → multi caches)."""
    from victron import VictronFleet
    c = cfg["victron"]
    raw_scan = c.get("scan_ids") or []
    if isinstance(raw_scan, int):
        scan_ids = [raw_scan]
    elif isinstance(raw_scan, str):
        scan_ids = [int(x.strip()) for x in raw_scan.split(",") if x.strip()] or None
    elif isinstance(raw_scan, list) and len(raw_scan) > 0:
        scan_ids = [int(x) for x in raw_scan if x]
    else:
        scan_ids = None

    fleet = VictronFleet(
        host=str(c.get("host", "192.168.1.50")),
        port=int(c.get("port", 502)),
        timeout=float(c.get("timeout", 5.0)),
        scan_unit_ids=scan_ids,
        poll_interval=cfg["general"]["poll_interval"],
    )
    try:
        await fleet.connect()
        devices = await fleet.scan()
        logger.info("Victron devices: %s", devices)
    except Exception as e:
        logger.error("Victron init: %s", e)

    # Vérifier si aucune source BMS n'est configurée
    has_bms = any(s.get("enabled", True) for s in cfg.get("bms_sources", []))

    while True:
        try:
            result = await asyncio.wait_for(fleet.poll_all(), timeout=45.0)
            _cache["inverter"]["units"] = result["inverters"]
            _cache["inverter"]["last_update"] = time.time()
            _cache["inverter"]["error"] = None
            _cache["inverter"]["type"] = "victron"
            _cache["solarchargers"]["units"] = result["solarchargers"]
            _cache["solarchargers"]["last_update"] = time.time()
            _cache["solarchargers"]["error"] = None

            # Batteries Victron → groupe séparé "victron_bat"
            if result["batteries"]:
                _cache["bms_groups"]["victron_bat"] = {
                    "name": "Victron Battery Monitor",
                    "type": "victron",
                    "units": result["batteries"],
                    "last_update": time.time(),
                    "error": None,
                }

            _cache["victron_system"]["data"] = result["system"]
            _cache["victron_system"]["last_update"] = time.time()
            _cache["victron_system"]["error"] = None

            # Inversion de signe au niveau du cache pour que le dashboard
            # (flèches VRM, onglet Onduleur, Grid/Batterie) soit cohérent
            sys_data = result["system"]
            vic_cfg = _cfg.get().get("victron", {}) if _cfg else {}
            inv_grid = vic_cfg.get("invert_grid_sign", False)
            inv_bat = vic_cfg.get("invert_battery_sign", False)

            if sys_data:
                if inv_grid and sys_data.get("grid_power") is not None:
                    sys_data["grid_power"] = -sys_data["grid_power"]
                if inv_bat and sys_data.get("battery_power") is not None:
                    sys_data["battery_power"] = -sys_data["battery_power"]

            # Inversion aussi par MultiPlus (ac_in = grid côté MultiPlus)
            if inv_grid:
                for mp in result["inverters"].values():
                    if mp.get("ac_in_power") is not None:
                        mp["ac_in_power"] = -mp["ac_in_power"]
                    if mp.get("ac_in_current") is not None:
                        mp["ac_in_current"] = -mp["ac_in_current"]

            if sys_data:
                logger.info("Victron: PV=%dW Conso=%dW Grid=%dW Bat=%s%%",
                            sys_data.get("total_pv_power", 0),
                            sys_data.get("consumption_power", 0),
                            sys_data.get("grid_power", 0),
                            sys_data.get("battery_soc", "?"))

            await broadcast({"type": "victron_update", "data": {
                "inverter": _cache["inverter"],
                "solarchargers": _cache["solarchargers"],
                "bms_groups": _cache["bms_groups"],
                "victron_system": _cache["victron_system"],
            }})
            _record_to_db()
            try:
                from health_tracker import get_tracker
                get_tracker().record("inverter_ok")
            except Exception:
                pass
        except Exception as e:
            _cache["inverter"]["error"] = str(e)
            logger.error("Victron: %s", e)
            try:
                from health_tracker import get_tracker
                get_tracker().record("inverter_fail", str(e))
            except Exception:
                pass
        await adaptive_sleep(cfg["general"]["poll_interval"])


async def bms_source_loop(source: dict, group_key: str, poll_interval: int):
    """Boucle pour une source BMS individuelle (pylontech ou jkbms)."""
    bms_type = source["type"]
    name = source.get("name", bms_type)

    # Initialiser le groupe dans le cache
    _cache["bms_groups"][group_key] = {
        "name": name, "type": bms_type,
        "units": {}, "last_update": None, "error": None,
    }

    if bms_type == "pylontech":
        from pylontech import PylontechPoller
        poller = PylontechPoller(
            source.get("host", "192.168.1.100"),
            source.get("port", 9999),
            source.get("num_batteries", 4),
        )
        while True:
            try:
                batteries = await asyncio.wait_for(poller.poll(), timeout=20.0)
                units = {str(b.battery_id): poller.to_dict(b) for b in batteries}
                _cache["bms_groups"][group_key]["units"] = units
                _cache["bms_groups"][group_key]["last_update"] = time.time()
                _cache["bms_groups"][group_key]["error"] = None
                logger.info("%s: %d batteries", name, len(batteries))
                await broadcast({"type": "bms_update", "data": {"bms_groups": _cache["bms_groups"]}})
                try:
                    from health_tracker import get_tracker
                    get_tracker().record("bms_ok")
                except Exception:
                    pass
            except Exception as e:
                _cache["bms_groups"][group_key]["error"] = str(e)
                logger.error("%s: %s", name, e)
                try:
                    from health_tracker import get_tracker
                    get_tracker().record("bms_fail", f"{name}: {e}")
                except Exception:
                    pass
            await adaptive_sleep(poll_interval)

    elif bms_type == "jkbms":
        from jkbms_modbus import JKBMSFleet, JKBMSModbusClient
        # Sécuriser le type des ids (peut être int, str, ou list selon la source)
        raw_ids = source.get("ids", [1])
        if isinstance(raw_ids, int):
            slave_ids = [raw_ids]
        elif isinstance(raw_ids, str):
            slave_ids = [int(x.strip()) for x in raw_ids.split(",") if x.strip()]
        elif isinstance(raw_ids, list):
            slave_ids = [int(x) for x in raw_ids if x]
        else:
            slave_ids = [1]

        fleet = JKBMSFleet(
            mode=str(source.get("mode", "tcp")),
            host=str(source.get("host", "192.168.1.100")),
            port=int(source.get("port", 502)),
            serial_port=str(source.get("serial_port", "/dev/ttyUSB0")),
            baudrate=int(source.get("baudrate", 115200)),
            slave_ids=slave_ids,
            timeout=float(source.get("timeout", 3.0)),
            poll_interval=poll_interval,
        )
        for client in fleet._clients.values():
            try:
                await client.connect()
            except Exception as e:
                logger.warning("%s init BMS %d: %s", name, client.slave_id, e)

        tmp = JKBMSModbusClient()
        while True:
            try:
                results = await asyncio.wait_for(fleet.poll_all(), timeout=30.0)
                units = {str(d.bms_id): tmp.to_dict(d) for d in results}
                _cache["bms_groups"][group_key]["units"] = units
                _cache["bms_groups"][group_key]["last_update"] = time.time()
                _cache["bms_groups"][group_key]["error"] = None
                await broadcast({"type": "bms_update", "data": {"bms_groups": _cache["bms_groups"]}})
            except Exception as e:
                _cache["bms_groups"][group_key]["error"] = str(e)
                logger.error("%s: %s", name, e)
            await adaptive_sleep(poll_interval)


async def finance_loop(cfg: dict):
    """Boucle du moteur financier."""
    from energy_finance import EnergyFinanceEngine, FinanceConfig
    fc = cfg["finance"]
    config = FinanceConfig(
        currency=fc.get("currency", "EUR"),
        contract_type=fc.get("contract_type", "fixed"),
        monthly_subscription=fc.get("monthly_subscription", 0),
        fixed_import_price=fc.get("import_price", 0),
        fixed_export_price=fc.get("export_price", 0),
        timezone=fc.get("timezone", "Europe/Paris"),
    )
    engine = EnergyFinanceEngine(config=config, data_dir=cfg["general"]["data_dir"])
    poll = cfg["general"]["poll_interval"]
    hours = poll / 3600.0

    while True:
        try:
            live = {"solar": 0, "load": 0, "grid_import": 0,
                    "grid_export": 0, "battery_charge": 0, "battery_discharge": 0}

            vicSystem = _cache["victron_system"].get("data")
            if vicSystem and vicSystem.get("online"):
                pv = sum(float(sc.get("pv_power", 0) or 0)
                         for sc in _cache["solarchargers"].get("units", {}).values()
                         if sc.get("online"))
                pv += float(vicSystem.get("pv_on_output_power", 0) or 0)
                pv += float(vicSystem.get("pv_on_grid_power", 0) or 0)
                ld = float(vicSystem.get("consumption_power", 0) or 0)
                gp = float(vicSystem.get("grid_power", 0) or 0)
                bp = float(vicSystem.get("battery_power", 0) or 0)
                live["solar"] += pv * hours / 1000
                live["load"] += ld * hours / 1000
                if gp > 0:
                    live["grid_import"] += gp * hours / 1000
                elif gp < 0:
                    live["grid_export"] += abs(gp) * hours / 1000
                if bp > 0:
                    live["battery_charge"] += bp * hours / 1000
                elif bp < 0:
                    live["battery_discharge"] += abs(bp) * hours / 1000
            else:
                for inv in _cache["inverter"].get("units", {}).values():
                    if not inv.get("online"):
                        continue
                    pv = float(inv.get("pv_input_power", 0) or 0)
                    ld = float(inv.get("output_active_power", 0) or 0)
                    bv = float(inv.get("battery_voltage", 0) or 0)
                    chg = float(inv.get("battery_charge_current", 0) or 0)
                    dis = float(inv.get("battery_discharge_current", 0) or 0)
                    live["solar"] += pv * hours / 1000
                    live["load"] += ld * hours / 1000
                    if chg > 0 and bv:
                        live["battery_charge"] += (chg * bv) * hours / 1000
                    if dis > 0 and bv:
                        live["battery_discharge"] += (dis * bv) * hours / 1000

            result = engine.update(live)
            _cache["finance"]["data"] = result
            _cache["finance"]["last_update"] = time.time()
            _cache["finance"]["error"] = None
            _cache["finance"]["enabled"] = True
            await broadcast({"type": "finance_update", "data": _cache["finance"]})
        except Exception as e:
            _cache["finance"]["error"] = str(e)
            logger.error("Finance: %s", e)
        await adaptive_sleep(poll)


def start_polling_tasks(cfg: dict) -> list:
    """Crée et retourne les tâches de polling selon la config."""
    tasks = []
    inv_type = cfg["inverter"]["type"]
    poll = cfg["general"]["poll_interval"]

    if inv_type == "voltronic":
        tasks.append(asyncio.create_task(inverter_loop(cfg)))
    elif inv_type == "victron":
        tasks.append(asyncio.create_task(victron_loop(cfg)))

    # Multi-BMS : une tâche par source activée
    for i, src in enumerate(cfg.get("bms_sources", [])):
        if not src.get("enabled", True):
            continue
        group_key = f"{src['type']}_{i}"
        tasks.append(asyncio.create_task(
            bms_source_loop(src, group_key, poll)
        ))

    if cfg["finance"].get("enabled", False):
        tasks.append(asyncio.create_task(finance_loop(cfg)))

    # Sprint 6 : boucle de push InfluxDB — démarrée inconditionnellement
    # (active/désactive sans restart, comme solax_loop)
    if _influx:
        tasks.append(asyncio.create_task(influx_loop()))

    # Sprint 8 : boucle de poll Solax — démarrée inconditionnellement.
    # La task vérifie `_solax.enabled` à chaque tick, ce qui permet d'activer
    # /désactiver Solax depuis les Réglages sans redémarrer le service.
    if _solax:
        tasks.append(asyncio.create_task(solax_loop()))

    return tasks


async def influx_loop():
    """Boucle de push InfluxDB."""
    while True:
        try:
            if _influx and _influx.enabled:
                await _influx.push(_cache)
            await asyncio.sleep(_influx.interval if _influx else 30)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"influx_loop error: {e}")
            await asyncio.sleep(30)


async def solax_loop():
    """Boucle de poll des onduleurs Solax."""
    while True:
        try:
            if _solax and _solax.enabled:
                await _solax.poll_once()
                # Met à jour le cache global pour que les autres modules
                # (Prometheus, InfluxDB, websocket) puissent y accéder
                _cache["solax"] = _solax.get_cache()
                # Sprint 11 : persister en DB le 1er onduleur (un seul stocké
                # en agrégat global pour ce sprint — multi-onduleurs viendra plus tard)
                if _db and _cache["solax"]:
                    try:
                        first_inv = next(iter(_cache["solax"].values()))
                        if first_inv.get("online") and first_inv.get("data"):
                            d = first_inv["data"]
                            _db.insert_solax_sample(
                                pv_power=d.get("pv_power", 0) or 0,
                                load_power=d.get("load_power", 0) or 0,
                                grid_power=d.get("grid_power", 0) or 0,
                                bat_power=d.get("battery_power", 0) or 0,
                                bat_soc=d.get("battery_soc", 0) or 0,
                                bat_voltage=d.get("battery_voltage", 0) or 0,
                                frequency=d.get("grid_frequency", 0) or 0,
                                temperature=d.get("inverter_temperature", 0) or 0,
                            )
                    except Exception as e:
                        logger.debug(f"Solax DB insert: {e}")
            await asyncio.sleep(_solax.poll_interval if _solax else 10)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"solax_loop error: {e}")
            await asyncio.sleep(15)


# ═══════════════════════════════════════════════════════════════════════════
#  Application FastAPI
# ═══════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db, _cfg, _tasks, _alerts, _mqtt, _forecast, _influx

    # ── Config ──
    from config_manager import ConfigManager
    _cfg = ConfigManager()
    _cfg.load()
    cfg = _cfg.get()

    # ── DB ──
    from database import EnergyDatabase
    _db = EnergyDatabase(os.path.join(cfg["general"]["data_dir"], "smart_energy_hub.db"))
    _db.init()

    # ── Sprint 10 : reset admin oublié + auth + setup wizard ──
    if _db.check_and_apply_reset_flag():
        logger.warning("Reset admin appliqué — wizard sera affiché au prochain accès")
    init_auth(_db.conn)
    _scanner = NetworkScanner()
    init_setup(_db, _cfg, _scanner)
    init_support(_cfg)
    auth = get_auth()
    logger.info("Auth: %d user(s), first_boot=%s",
                auth.count_users(), _db.is_first_boot())

    # ── Alertes Telegram (lazy : ne pas importer si désactivé) ──
    if cfg.get("alerts", {}).get("enabled"):
        from alerts import AlertManager
        _alerts = AlertManager(cfg.get("alerts", {}))
        if _alerts.enabled:
            logger.info("Alertes Telegram activées (chat_id=%s)", _alerts.chat_id)
    else:
        _alerts = None
        logger.debug("Alertes désactivées — module non chargé (gain RAM)")

    # ── MQTT (lazy) ──
    if cfg.get("mqtt", {}).get("enabled"):
        from mqtt_publisher import MQTTPublisher
        _mqtt = MQTTPublisher(cfg.get("mqtt", {}))
        if _mqtt.enabled:
            _mqtt.connect()
            logger.info("MQTT activé → %s:%s", cfg["mqtt"].get("broker"), cfg["mqtt"].get("port"))
    else:
        _mqtt = None
        logger.debug("MQTT désactivé — module non chargé (gain RAM)")

    # ── Solar Forecast (lazy) ──
    if cfg.get("solar_forecast", {}).get("enabled"):
        from solar_forecast import SolarForecast
        _forecast = SolarForecast(cfg.get("solar_forecast", {}))
        if _forecast.enabled:
            logger.info("Prévisions solaires activées (%.2f°N, %.2f°E)",
                        cfg["solar_forecast"].get("latitude", 0),
                        cfg["solar_forecast"].get("longitude", 0))
    else:
        _forecast = None
        logger.debug("Forecast désactivé — module non chargé (gain RAM)")

    # ── InfluxDB Publisher (lazy) ──
    global _influx
    if cfg.get("integrations", {}).get("influxdb_enabled"):
        from influx_publisher import InfluxPublisher
        _influx = InfluxPublisher(cfg.get("integrations", {}))
        if _influx.enabled:
            logger.info("InfluxDB activé → %s (bucket=%s, interval=%ds)",
                        cfg["integrations"].get("influxdb_url"),
                        cfg["integrations"].get("influxdb_bucket", "solar"),
                        _influx.interval)
    else:
        _influx = None
        logger.debug("InfluxDB désactivé — module non chargé (gain RAM)")

    # ── Solax Fleet (lazy) ──
    global _solax
    if cfg.get("solax", {}).get("enabled"):
        from solax_fleet import SolaxFleet
        _solax = SolaxFleet(cfg.get("solax", {}))
        if _solax.enabled:
            n = len(cfg.get("solax", {}).get("inverters", []))
            logger.info(f"Solax fleet activé ({n} onduleur(s) configuré(s))")
    else:
        _solax = None
        logger.debug("Solax désactivé — module non chargé (gain RAM)")

    # ── Sprint 13 : moniteur de performances ──
    from perf_monitor import init_monitor
    _perf = init_monitor()
    logger.info("PerfMonitor: %s", _perf.info())

    # ── Sprint 15 : Health tracker ──
    from health_tracker import get_tracker
    get_tracker()  # init + record("app_start")
    logger.info("HealthTracker initialisé")

    # ── Update system info ──
    _cache["system"] = {
        "inverter_type": cfg["inverter"]["type"],
        "bms_sources": [s.get("name", s["type"]) for s in cfg.get("bms_sources", []) if s.get("enabled", True)],
        "finance_enabled": cfg["finance"].get("enabled", False),
        "poll_interval": cfg["general"]["poll_interval"],
        "uptime_start": time.time(),
        "config_version": _cfg.version,
    }

    # ── Start polling ──
    _tasks = start_polling_tasks(cfg)

    bms_names = ", ".join(s.get("name", s["type"]) for s in cfg.get("bms_sources", []) if s.get("enabled", True)) or "none"
    logger.info("Smart Energy Hub démarré — Inverter: %s | BMS: [%s] | Finance: %s",
                cfg["inverter"]["type"], bms_names, cfg["finance"].get("enabled"))
    yield

    for task in _tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if _db:
        _db.close()
    if _mqtt:
        _mqtt.disconnect()


app = FastAPI(title="Smart Energy Hub", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ═══════════════════════════════════════════════════════════════════════════
#  Sprint 10 — AuthMiddleware + routers auth/setup/support
# ═══════════════════════════════════════════════════════════════════════════

# Préfixes API publics (jamais d'auth requise)
_PUBLIC_API_PREFIXES = (
    "/api/auth/login",
    "/api/auth/logout",
    "/api/setup/",      # wizard accessible en mode bootstrap
    "/api/support/",    # formulaire contact accessible (anti-lockout)
    "/health",
    "/metrics",         # Prometheus scraping
)


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware global :
    - En mode bootstrap (0 user) → toutes routes ouvertes (le wizard a besoin de fonctionner)
    - Sinon → /api/* exigent un cookie JWT valide (sauf préfixes publics)
    - Routes statiques (frontend HTML/JS/CSS) toujours servies
    - WebSocket /ws : auth gérée séparément (vérif cookie au handshake)
    """

    async def dispatch(self, request, call_next):
        path = request.url.path

        # WebSocket et statiques toujours OK (auth WS gérée dans le handler)
        if path == "/ws" or not path.startswith("/api/"):
            return await call_next(request)

        # Préfixes publics OK
        if path.startswith(_PUBLIC_API_PREFIXES):
            return await call_next(request)

        # Mode bootstrap : aucun user → on laisse tout passer
        try:
            am = get_auth()
            if not am.has_any_user():
                return await call_next(request)
        except RuntimeError:
            # Auth pas encore initialisée (lifespan en cours) → on laisse passer
            return await call_next(request)

        # Vérif cookie
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return JSONResponse(status_code=401, content={"detail": "Non authentifié"})
        user = am.user_from_token(token)
        if not user:
            return JSONResponse(status_code=401, content={"detail": "Session invalide ou expirée"})
        request.state.user = user
        return await call_next(request)


app.add_middleware(AuthMiddleware)
app.include_router(auth_router)
app.include_router(setup_router)
app.include_router(support_router)


# ── API REST ──

@app.get("/api/status")
async def get_status():
    return {
        "inverter": _cache["inverter"],
        "bms_groups": _cache["bms_groups"],
        "solarchargers": _cache["solarchargers"],
        "victron_system": _cache["victron_system"],
        "solax": _cache.get("solax", {}),
        "finance": _cache["finance"],
        "system": {**_cache["system"], "uptime": int(time.time() - _cache["system"]["uptime_start"])},
    }


# ── Sprint 13 : performance monitoring ──

@app.get("/api/perf")
async def get_perf():
    """Snapshot perf actuel + infos système statiques."""
    from perf_monitor import get_monitor
    m = get_monitor()
    snap = m.snapshot()
    return {
        "info": m.info(),
        "current": snap.to_dict() if snap else None,
        "throttled": m.should_throttle(),
        "throttle_factor": m.get_throttle_factor(),
    }


@app.get("/api/perf/history")
async def get_perf_history(minutes: int = 30):
    """Historique des `minutes` dernières minutes (par tranches de 5s)."""
    from perf_monitor import get_monitor
    n = max(1, min(720, int(minutes * 60 / 5)))
    return {"samples": get_monitor().history(last_n=n)}


# ── Sprint 14 : OTA updates ──

@app.get("/api/update/status")
async def get_update_status(_user: User = Depends(get_current_user)):
    """État courant : version installée + statut MAJ en cours/passée."""
    from updater import get_current_version, get_status
    return {
        "current": get_current_version().to_dict(),
        "status": get_status().to_dict(),
    }


@app.get("/api/update/check")
async def check_update(channel: str = "stable", force: bool = False,
                       _admin: User = Depends(get_current_admin)):
    """Vérifie si une nouvelle version est disponible sur GitHub."""
    if channel not in ("stable", "dev"):
        raise HTTPException(status_code=400, detail="channel invalide (stable|dev)")
    from updater import get_updater
    return get_updater().check(channel=channel, force=force)


class _UpdateApplyReq(BaseModel):
    channel: str = "stable"
    target_version: Optional[str] = None  # si fourni, force cette version


@app.post("/api/update/apply")
async def apply_update(req: _UpdateApplyReq, _admin: User = Depends(get_current_admin)):
    """Demande l'application d'une MAJ (le script hôte fera le travail)."""
    if req.channel not in ("stable", "dev"):
        raise HTTPException(status_code=400, detail="channel invalide")
    from updater import get_updater
    try:
        status = get_updater().request_update(req.channel, req.target_version)
        return {"ok": True, "status": status.to_dict()}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/api/update/rollback")
async def rollback_update(_admin: User = Depends(get_current_admin)):
    """Revient à la version précédente."""
    from updater import get_updater
    try:
        status = get_updater().request_rollback()
        return {"ok": True, "status": status.to_dict()}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/update/cancel")
async def cancel_update(_admin: User = Depends(get_current_admin)):
    """Annule une MAJ pending (si pas déjà commencée)."""
    from updater import get_updater
    ok = get_updater().cancel_pending()
    return {"ok": ok, "message": "MAJ annulée" if ok else "Aucune MAJ pending"}


# ── Sprint 14 : Diagnostics pour support ──

@app.get("/api/diagnostics")
async def get_diagnostics(_admin: User = Depends(get_current_admin)):
    """
    Renvoie un export sanitizé pour le support technique :
    version, infos système, perf récentes, modules activés, derniers logs.
    Pas de mots de passe, pas de tokens.
    """
    from updater import get_current_version, get_status
    from perf_monitor import get_monitor

    cfg = _cfg.get() if _cfg else {}
    sanitized_cfg = _sanitize_config(cfg)

    return {
        "generated_at": time.time(),
        "version": get_current_version().to_dict(),
        "update_status": get_status().to_dict(),
        "perf": {
            "info": get_monitor().info(),
            "current": (get_monitor().snapshot().to_dict() if get_monitor().snapshot() else None),
            "history_30min": get_monitor().history(last_n=360),  # 30 min
        },
        "modules_enabled": _list_enabled_modules(cfg),
        "config_sanitized": sanitized_cfg,
        "uptime_s": int(time.time() - _cache["system"].get("uptime_start", time.time())),
        "auth_users_count": get_auth().count_users(),
        "first_boot": _db.is_first_boot() if _db else None,
    }


def _sanitize_config(cfg: dict) -> dict:
    """Retire mots de passe, tokens, et autres secrets de la config."""
    SENSITIVE = {"password", "smtp_password", "token", "telegram_token",
                 "influxdb_token", "api_key", "secret"}
    def _walk(d):
        if isinstance(d, dict):
            return {k: ("[REDACTED]" if any(s in k.lower() for s in SENSITIVE) else _walk(v))
                    for k, v in d.items()}
        if isinstance(d, list):
            return [_walk(x) for x in d]
        return d
    return _walk(cfg)


def _list_enabled_modules(cfg: dict) -> list:
    enabled = []
    if cfg.get("inverter", {}).get("type", "none") != "none":
        enabled.append(f"inverter:{cfg['inverter']['type']}")
    for s in cfg.get("bms_sources", []):
        if s.get("enabled", True):
            enabled.append(f"bms:{s.get('type','?')}")
    for k in ("alerts", "mqtt", "solar_forecast", "solax", "finance"):
        if cfg.get(k, {}).get("enabled"):
            enabled.append(k)
    if cfg.get("integrations", {}).get("influxdb_enabled"):
        enabled.append("influxdb")
    return enabled


# ── Sprint 15 : Logs viewer (lecture seule, whitelistée) ──

@app.get("/api/logs/sources")
async def list_log_sources(_admin: User = Depends(get_current_admin)):
    """Liste les sources de logs accessibles (whitelist en dur)."""
    from log_reader import list_sources
    return {"sources": list_sources()}


@app.get("/api/logs/{source}")
async def read_log_source(source: str, lines: int = 200,
                          since: Optional[str] = None,
                          _admin: User = Depends(get_current_admin)):
    """
    Lit les logs d'une source whitelistée.
    Sources : app | network | update | watchdog
    `lines` : 1-500
    `since` : optionnel, ex "1h", "30m"
    """
    from log_reader import read_logs
    result = read_logs(source, lines=lines, since=since)
    return result.to_dict()


# ── Sprint 15 : Health dashboard ──

@app.get("/api/health/dashboard")
async def health_dashboard(_user: User = Depends(get_current_user)):
    """Snapshot temps réel + buckets horaires sur 24h."""
    from health_tracker import get_tracker
    tracker = get_tracker()
    return {
        "snapshot": tracker.snapshot(),
        "hourly": tracker.hourly_buckets(),
        "recent_failures": tracker.recent_failures(max_n=20),
    }


# ── Sprint 15 : Support export ZIP ──

@app.get("/api/support/export")
async def support_export(_admin: User = Depends(get_current_admin)):
    """
    Génère un ZIP avec diagnostics + logs + santé. À joindre à un email support.
    """
    from log_reader import LOG_SOURCES, read_logs
    from health_tracker import get_tracker
    from support_export import build_support_zip
    from fastapi.responses import Response

    # Préparer les données
    cfg = _cfg.get() if _cfg else {}
    diag = await get_diagnostics(_admin=_admin)  # réutilise la même logique sanitisée

    tracker = get_tracker()
    snap = tracker.snapshot()
    fails = tracker.recent_failures(max_n=30)
    events = tracker.all_events()

    # Logs : 500 lignes par source (ou 2000 en mode export)
    logs_by_source = {}
    for src_id in LOG_SOURCES:
        result = read_logs(src_id, lines=1000, for_export=True)
        if result.available:
            logs_by_source[src_id] = result.lines
        else:
            logs_by_source[src_id] = [f"(indisponible : {result.error})"]

    zip_bytes = build_support_zip(diag, snap, events, fails, logs_by_source)
    filename = f"seh-support-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(zip_bytes)),
        },
    )


@app.get("/api/inverter")
async def get_inverter():
    return _cache["inverter"]


@app.get("/api/bms")
async def get_bms():
    """Retourne tous les groupes BMS."""
    return _cache["bms_groups"]


@app.get("/api/solarchargers")
async def get_solarchargers():
    return _cache["solarchargers"]


@app.get("/api/victron_system")
async def get_victron_system():
    return _cache["victron_system"]


@app.get("/api/finance")
async def get_finance():
    return _cache["finance"]


# ── API Config (Settings) ──

@app.get("/api/settings")
async def get_settings():
    """Retourne la config éditable."""
    if not _cfg:
        return {"error": "Config non initialisée"}
    return {"config": _cfg.get(), "version": _cfg.version}


@app.post("/api/settings")
async def save_settings(request: Request):
    """Sauvegarde la config et signale un redémarrage des boucles."""
    if not _cfg:
        return JSONResponse({"error": "Config non initialisée"}, status_code=500)
    try:
        body = await request.json()
        new_cfg = _cfg.update(body)

        # Mettre à jour le system info
        _cache["system"]["inverter_type"] = new_cfg["inverter"]["type"]
        _cache["system"]["bms_sources"] = [
            s.get("name", s["type"]) for s in new_cfg.get("bms_sources", [])
            if s.get("enabled", True)
        ]
        _cache["system"]["finance_enabled"] = new_cfg["finance"].get("enabled", False)
        _cache["system"]["poll_interval"] = new_cfg["general"]["poll_interval"]
        _cache["system"]["config_version"] = _cfg.version

        # Rafraîchir les modules avec la nouvelle config (sans restart)
        if _alerts:
            _alerts.update_config(new_cfg.get("alerts", {}))
        if _mqtt:
            _mqtt.update_config(new_cfg.get("mqtt", {}))
        if _forecast:
            _forecast.update_config(new_cfg.get("solar_forecast", {}))
        if _influx:
            _influx.update_config(new_cfg.get("integrations", {}))
        if _solax:
            _solax.update_config(new_cfg.get("solax", {}))

        return {
            "status": "saved",
            "version": _cfg.version,
            "message": "Config sauvegardée. Alertes/MQTT/Prévisions mis à jour. Redémarrer pour les changements onduleur/BMS.",
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/settings/bms_sources")
async def get_bms_sources():
    """Liste les sources BMS configurées."""
    if not _cfg:
        return []
    return _cfg.get().get("bms_sources", [])


@app.post("/api/settings/bms_sources")
async def save_bms_sources(request: Request):
    """Met à jour les sources BMS."""
    body = await request.json()
    if not isinstance(body, list):
        return JSONResponse({"error": "Expected a list"}, status_code=400)
    _cfg.update({"bms_sources": body})
    return {"status": "saved", "bms_sources": body,
            "message": "Redémarrer le conteneur pour appliquer."}


# ── API Historique ──

@app.get("/api/history/realtime")
async def get_history_realtime(hours: int = 24):
    if not _db:
        return {"data": []}
    return {"data": _db.get_realtime(hours)}


@app.get("/api/history/hourly")
async def get_history_hourly(days: int = 30):
    if not _db:
        return {"data": []}
    return {"data": _db.get_hourly(days)}


@app.get("/api/history/daily")
async def get_history_daily(days: int = 365):
    if not _db:
        return {"data": []}
    return {"data": _db.get_daily(days)}


@app.get("/api/history/stats")
async def get_history_stats():
    if not _db:
        return {}
    return _db.get_stats_summary()


@app.get("/api/today_vs_yesterday")
async def get_today_vs_yesterday():
    """
    Résumé des totaux du jour en cours vs hier.
    Inclut PV, conso, import, export, batterie, autosuffisance, économies (si finance configurée).
    """
    if not _db:
        return {"error": "DB non initialisée"}

    import datetime
    now = datetime.datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    # Récupérer les 2 derniers jours
    daily = _db.get_daily(days=7)  # 7j au cas où il y aurait des trous
    by_date = {d["date"]: d for d in daily}

    today = by_date.get(today_str, {})
    yesterday = by_date.get(yesterday_str, {})

    # Prix pour calcul économies
    import_price = 0.0
    export_price = 0.0
    if _cfg:
        fin = _cfg.get().get("finance", {})
        import_price = float(fin.get("import_price", 0) or 0)
        export_price = float(fin.get("export_price", 0) or 0)

    def enrich(d):
        if not d:
            return {"empty": True}
        pv = d.get("pv_kwh", 0) or 0
        load = d.get("load_kwh", 0) or 0
        imp = d.get("import_kwh", 0) or 0
        exp = d.get("export_kwh", 0) or 0
        self_suff = d.get("self_sufficiency", 0)
        if not self_suff and load > 0:
            self_suff = round((load - imp) / load * 100, 1)
        savings = (load * import_price - imp * import_price) + exp * export_price
        return {
            "date": d.get("date"),
            "pv_kwh": round(pv, 2),
            "load_kwh": round(load, 2),
            "import_kwh": round(imp, 2),
            "export_kwh": round(exp, 2),
            "bat_charge_kwh": round(d.get("bat_charge_kwh", 0) or 0, 2),
            "bat_discharge_kwh": round(d.get("bat_discharge_kwh", 0) or 0, 2),
            "self_sufficiency": round(self_suff or 0, 1),
            "savings": round(savings, 2),
            "soc_min": d.get("soc_min"),
            "soc_max": d.get("soc_max"),
        }

    def delta(cur, prev, key):
        c = cur.get(key, 0) if not cur.get("empty") else 0
        p = prev.get(key, 0) if not prev.get("empty") else 0
        diff = c - p
        pct = round(diff / p * 100, 1) if p else None
        return {"diff": round(diff, 2), "pct": pct}

    today_d = enrich(today)
    yesterday_d = enrich(yesterday)
    deltas = {k: delta(today_d, yesterday_d, k)
              for k in ["pv_kwh", "load_kwh", "import_kwh", "export_kwh",
                        "savings", "self_sufficiency"]}

    # Agrégats mois en cours et année en cours (pour les widgets dashboard)
    month_prefix = now.strftime("%Y-%m")
    year_prefix = now.strftime("%Y")
    # Récupère plus de jours pour couvrir au moins l'année
    daily_year = _db.get_daily(days=400)
    month_pv = sum((d.get("pv_kwh", 0) or 0) for d in daily_year if d.get("date", "").startswith(month_prefix))
    month_load = sum((d.get("load_kwh", 0) or 0) for d in daily_year if d.get("date", "").startswith(month_prefix))
    month_import = sum((d.get("import_kwh", 0) or 0) for d in daily_year if d.get("date", "").startswith(month_prefix))
    month_export = sum((d.get("export_kwh", 0) or 0) for d in daily_year if d.get("date", "").startswith(month_prefix))
    year_pv = sum((d.get("pv_kwh", 0) or 0) for d in daily_year if d.get("date", "").startswith(year_prefix))
    year_load = sum((d.get("load_kwh", 0) or 0) for d in daily_year if d.get("date", "").startswith(year_prefix))
    year_import = sum((d.get("import_kwh", 0) or 0) for d in daily_year if d.get("date", "").startswith(year_prefix))
    year_export = sum((d.get("export_kwh", 0) or 0) for d in daily_year if d.get("date", "").startswith(year_prefix))

    return {
        "today": today_d,
        "yesterday": yesterday_d,
        "deltas": deltas,
        "month": {
            "pv_kwh": round(month_pv, 1),
            "load_kwh": round(month_load, 1),
            "import_kwh": round(month_import, 1),
            "export_kwh": round(month_export, 1),
        },
        "year": {
            "pv_kwh": round(year_pv, 1),
            "load_kwh": round(year_load, 1),
            "import_kwh": round(year_import, 1),
            "export_kwh": round(year_export, 1),
        },
        "currency": (_cfg.get().get("finance", {}).get("currency", "EUR")
                     if _cfg else "EUR"),
    }


@app.get("/api/heatmap/pv")
async def get_heatmap_pv(days: int = 365):
    """
    Retourne tous les jours avec leur pv_kwh pour afficher une heatmap annuelle type GitHub.
    """
    if not _db:
        return {"data": [], "max": 0}
    daily = _db.get_daily(days=days)
    data = [{"date": d["date"], "ts": d["ts"],
             "pv_kwh": round(d.get("pv_kwh", 0) or 0, 2),
             "load_kwh": round(d.get("load_kwh", 0) or 0, 2),
             "import_kwh": round(d.get("import_kwh", 0) or 0, 2),
             "export_kwh": round(d.get("export_kwh", 0) or 0, 2),
             "self_sufficiency": d.get("self_sufficiency", 0)}
            for d in daily]
    max_pv = max((d["pv_kwh"] for d in data), default=0)
    # Stats globales utiles
    total_pv = sum(d["pv_kwh"] for d in data)
    total_export = sum(d["export_kwh"] for d in data)
    best_day = max(data, key=lambda d: d["pv_kwh"]) if data else None
    return {
        "data": data,
        "max_pv": round(max_pv, 2),
        "avg_pv": round(total_pv / len(data), 2) if data else 0,
        "total_pv": round(total_pv, 2),
        "total_export": round(total_export, 2),
        "days": len(data),
        "best_day": best_day,
    }


@app.get("/api/battery_health")
async def get_battery_health():
    """
    Analyse l'état de santé et la durée de vie restante estimée pour chaque batterie.
    Basé sur cycle_count, SoH, et hypothèse LiFePO4 : 6000 cycles @ 80% DoD avant 80% SoH.
    """
    groups = _cache.get("bms_groups", {})
    CYCLES_EOL = 6000  # LiFePO4 typique (JK-BMS), EoL conventionnel à 80% SoH
    CYCLES_EOL_PYLON = 4500  # Pylontech US2000B : ~4500 cycles selon datasheet

    report = []
    for group_id, group in groups.items():
        group_type = group.get("type", "unknown")
        cycles_ref = CYCLES_EOL_PYLON if group_type == "pylontech" else CYCLES_EOL
        for bat_id, bat in group.get("units", {}).items():
            if not bat.get("online"):
                continue
            cycles = bat.get("cycle_count")
            soh = bat.get("soh")  # dispo uniquement JK-BMS

            # Estimation simple : cycles restants
            cycles_remaining = None
            cycles_pct = None
            years_remaining = None
            if cycles is not None:
                cycles_remaining = max(0, cycles_ref - cycles)
                cycles_pct = round(cycles / cycles_ref * 100, 1)
                # Estimation : 1 cycle complet / jour en moyenne (approximation)
                # À affiner avec l'historique réel si dispo
                years_remaining = round(cycles_remaining / 365, 1) if cycles_remaining else 0

            # Estimation SoH si pas fourni (linéaire basé sur cycles)
            soh_estimated = None
            if soh is None and cycles is not None:
                # 100% à 0 cycles, 80% à CYCLES_EOL
                soh_estimated = round(100 - (cycles / cycles_ref) * 20, 1)

            # État santé
            effective_soh = soh if soh is not None else soh_estimated
            if effective_soh is None:
                health_status = "unknown"
            elif effective_soh >= 95:
                health_status = "excellent"
            elif effective_soh >= 90:
                health_status = "good"
            elif effective_soh >= 85:
                health_status = "fair"
            elif effective_soh >= 80:
                health_status = "warning"
            else:
                health_status = "critical"

            report.append({
                "group_id": group_id,
                "group_name": group.get("name", group_id),
                "group_type": group_type,
                "id": bat_id,
                "label": f"{group.get('name', group_id)} #{bat_id}",
                "cycles": cycles,
                "cycles_ref": cycles_ref,
                "cycles_remaining": cycles_remaining,
                "cycles_pct": cycles_pct,
                "years_remaining_est": years_remaining,
                "soh_reported": soh,
                "soh_estimated": soh_estimated,
                "soh_effective": effective_soh,
                "health_status": health_status,
                "voltage": bat.get("voltage"),
                "nominal_capacity": bat.get("nominal_capacity"),
                "remaining_capacity": bat.get("remaining_capacity"),
            })

    # Agrégats globaux
    if report:
        avg_soh = sum(r["soh_effective"] for r in report if r["soh_effective"]) / \
                  max(1, len([r for r in report if r["soh_effective"]]))
        avg_cycles = sum(r["cycles"] for r in report if r["cycles"]) / \
                     max(1, len([r for r in report if r["cycles"]]))
        worst = min(report, key=lambda r: r["soh_effective"] or 100)
    else:
        avg_soh = None
        avg_cycles = None
        worst = None

    return {
        "batteries": report,
        "summary": {
            "count": len(report),
            "avg_soh": round(avg_soh, 1) if avg_soh else None,
            "avg_cycles": round(avg_cycles) if avg_cycles else None,
            "worst_battery": worst["label"] if worst else None,
            "worst_soh": worst["soh_effective"] if worst else None,
        },
    }


# ── Healthcheck ──

@app.get("/health")
async def health():
    """Endpoint Docker healthcheck : vérifie que les composants critiques fonctionnent."""
    import datetime
    status = {"status": "ok", "checks": {}}
    now = time.time()
    issues = []

    # DB
    if _db and _db._conn:
        try:
            _db._conn.execute("SELECT 1").fetchone()
            status["checks"]["db"] = "ok"
        except Exception as e:
            status["checks"]["db"] = f"error: {e}"
            issues.append("db")
    else:
        status["checks"]["db"] = "not_initialized"
        issues.append("db")

    # Polling : au moins une source doit avoir été mise à jour récemment
    last_updates = []
    inv_last = _cache["inverter"].get("last_update")
    vic_last = _cache["victron_system"].get("last_update")
    if inv_last:
        last_updates.append(("inverter", inv_last))
    if vic_last:
        last_updates.append(("victron_system", vic_last))
    for gid, g in _cache.get("bms_groups", {}).items():
        if g.get("last_update"):
            last_updates.append((f"bms_{gid}", g["last_update"]))

    if last_updates:
        most_recent = max(last_updates, key=lambda x: x[1])
        age = now - most_recent[1]
        if age < 300:  # < 5 min
            status["checks"]["polling"] = f"ok (last: {most_recent[0]} {age:.0f}s ago)"
        else:
            status["checks"]["polling"] = f"stale (last update {age:.0f}s ago)"
            issues.append("polling_stale")
    else:
        # Acceptable : aucune source configurée, pas d'erreur
        status["checks"]["polling"] = "no_sources_configured"

    # Config
    if _cfg:
        status["checks"]["config"] = f"ok (v{_cfg.version})"
    else:
        status["checks"]["config"] = "not_initialized"
        issues.append("config")

    # Uptime
    uptime = int(now - _cache["system"]["uptime_start"])
    status["uptime_seconds"] = uptime

    if issues:
        status["status"] = "degraded" if "polling_stale" in issues else "error"
        status["issues"] = issues
        return JSONResponse(status_code=503 if "db" in issues or "config" in issues else 200,
                            content=status)

    return status


# ── Export CSV/JSON ──

from fastapi.responses import StreamingResponse
import csv
import io

@app.get("/api/export/{period}")
async def export_data(period: str, format: str = "csv"):
    """Export des données historiques en CSV ou JSON.
    period: realtime | hourly | daily
    format: csv | json
    """
    if not _db:
        return JSONResponse({"error": "DB non initialisée"}, status_code=500)

    if period == "realtime":
        data = _db.get_realtime(24)
    elif period == "hourly":
        data = _db.get_hourly(30)
    elif period == "daily":
        data = _db.get_daily(365)
    else:
        return JSONResponse({"error": "Période invalide (realtime/hourly/daily)"}, status_code=400)

    if not data:
        return JSONResponse({"error": "Aucune donnée"}, status_code=404)

    if format == "json":
        return JSONResponse({"period": period, "count": len(data), "data": data},
                           headers={"Content-Disposition": f"attachment; filename=seh_{period}.json"})

    # CSV
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=data[0].keys())
    writer.writeheader()
    for row in data:
        # Convertir le timestamp en date lisible
        from datetime import datetime
        row_copy = dict(row)
        if "ts" in row_copy:
            row_copy["datetime"] = datetime.fromtimestamp(row_copy["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow(row_copy)
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=seh_{period}.csv"}
    )


# ── API Solar Forecast ──

@app.get("/api/forecast")
async def get_forecast():
    """Prévisions de production solaire sur 5 jours.
    Persiste automatiquement un snapshot dans la DB pour mesure de précision ultérieure."""
    if not _forecast or not _forecast.enabled:
        return {"error": "Prévisions solaires désactivées. Configurez dans ⚙️ Réglages."}
    try:
        data = await _forecast.fetch_forecast()
        # Sauvegarde pour mesure de précision (idempotent, écrase la dernière du jour)
        if _db and data.get("daily"):
            try:
                _db.save_forecast_snapshot(data["daily"])
            except Exception as e:
                logger.warning(f"Impossible de sauvegarder le snapshot forecast: {e}")
        return data
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/forecast/accuracy")
async def get_forecast_accuracy(days: int = 60):
    """
    Historique de la précision du modèle de prévision solaire.
    Compare les prévisions passées (archivées) avec les productions réelles mesurées.
    Retourne la série + metrics (MAE, MAPE, R²).
    """
    if not _db:
        return {"error": "DB non disponible"}

    series = _db.get_forecast_vs_actual(days=days)

    if not series:
        return {
            "series": [],
            "metrics": None,
            "message": "Pas encore d'historique de prévisions disponibles. "
                       "Le module persiste les prévisions automatiquement "
                       "à chaque consultation de /api/forecast. "
                       "Compte ~2-3 jours pour avoir les premières comparaisons."
        }

    # Metrics
    n = len(series)
    errors_kwh = [s["error_kwh"] for s in series]
    abs_errors = [abs(e) for e in errors_kwh]
    pct_errors = [abs(s["error_pct"]) for s in series if s["error_pct"] is not None]

    mae = sum(abs_errors) / n  # Mean Absolute Error (kWh)
    bias = sum(errors_kwh) / n  # moyenne signée : + si réel > prévu, - sinon
    mape = (sum(pct_errors) / len(pct_errors)) if pct_errors else None

    # R² (coefficient de détermination)
    actuals = [s["actual_kwh"] for s in series]
    mean_actual = sum(actuals) / n
    ss_tot = sum((a - mean_actual) ** 2 for a in actuals)
    ss_res = sum(e ** 2 for e in errors_kwh)
    r2 = 1 - (ss_res / ss_tot) if ss_tot > 0 else None

    # Classification (pour aider l'utilisateur)
    if mape is None:
        quality = "unknown"
    elif mape < 15:
        quality = "excellent"
    elif mape < 25:
        quality = "good"
    elif mape < 40:
        quality = "fair"
    else:
        quality = "poor"

    return {
        "series": series,
        "metrics": {
            "days_compared": n,
            "mae_kwh": round(mae, 2),
            "bias_kwh": round(bias, 2),
            "mape_pct": round(mape, 1) if mape is not None else None,
            "r2": round(r2, 3) if r2 is not None else None,
            "quality": quality,
            "total_forecast_kwh": round(sum(s["forecast_kwh"] for s in series), 1),
            "total_actual_kwh": round(sum(actuals), 1),
        }
    }


# ── API Leaf (optimiseur charge VE) ──

@app.get("/api/leaf/optimize")
async def leaf_optimize(day_offset: int = 1):
    """
    Calcule le créneau de charge optimal pour la Leaf sur un jour donné.
    day_offset : 0 = aujourd'hui (reste), 1 = demain, 2 = après-demain

    Algorithme :
      1. Récupère la prévision horaire PV pour le jour cible
      2. Soustrait la consommation moyenne de la maison (depuis samples_daily)
      3. Identifie les heures avec surplus PV > seuil de charge VE
      4. Sélectionne le créneau continu le plus productif dans la fenêtre pref
      5. Calcule kWh à charger, durée, heure optimale de départ
    """
    if not _cfg:
        return {"error": "Config non disponible"}
    cfg = _cfg.get()
    leaf = cfg.get("leaf", {})

    if not leaf.get("enabled"):
        return {"enabled": False, "message": "Module Leaf désactivé. Active-le dans Réglages."}

    bat_kwh = float(leaf.get("battery_capacity_kwh", 24))
    charge_kw = float(leaf.get("charge_power_kw", 3.6))
    target_soc = float(leaf.get("target_soc_percent", 80))
    current_soc = float(leaf.get("current_soc_percent", 50))
    min_charge = float(leaf.get("min_charge_kwh", 3))
    allow_grid = bool(leaf.get("allow_grid_import", False))
    pref_start = int(leaf.get("preferred_start_hour", 10))
    pref_end = int(leaf.get("preferred_end_hour", 17))

    # kWh à charger
    needed_kwh = max(0, (target_soc - current_soc) / 100 * bat_kwh)
    if needed_kwh < 0.5:
        return {
            "enabled": True,
            "message": "Batterie déjà proche de la cible, aucune charge nécessaire.",
            "needed_kwh": round(needed_kwh, 2),
        }

    # Récupérer la prévision horaire
    if not _forecast or not _forecast.enabled:
        return {"enabled": True, "error": "Prévisions solaires désactivées. Active-les dans ⚙️ Réglages."}
    fc = _forecast.get_cached()
    if not fc or not fc.get("hourly"):
        fc = await _forecast.fetch_forecast()

    import datetime as dt
    now = dt.datetime.now()
    target_date = (now + dt.timedelta(days=day_offset)).strftime("%Y-%m-%d")

    # Filtre les heures du jour cible dans la fenêtre préférée
    hourly = fc.get("hourly", [])
    day_hours = []
    for h in hourly:
        try:
            hdt = dt.datetime.fromtimestamp(h["ts"])
            if hdt.strftime("%Y-%m-%d") != target_date:
                continue
            if pref_start <= hdt.hour <= pref_end:
                day_hours.append({
                    "hour": hdt.hour,
                    "datetime": h["datetime"],
                    "ts": h["ts"],
                    "pv_kwh": h.get("wh", 0) / 1000,
                })
        except Exception:
            continue

    if not day_hours:
        return {
            "enabled": True,
            "error": f"Pas de prévision disponible pour {target_date} entre {pref_start}h et {pref_end}h",
            "needed_kwh": round(needed_kwh, 2),
        }

    # Consommation moyenne par heure du jour (issue de samples_daily) pour soustraire l'usage maison
    avg_hourly_load = 0.5  # kW défaut si pas de data
    if _db:
        daily = _db.get_daily(days=14)
        if daily:
            total_load = sum(d.get("load_kwh", 0) or 0 for d in daily)
            avg_hourly_load = total_load / (len(daily) * 24) if daily else 0.5

    # Surplus prévu par heure
    for h in day_hours:
        h["surplus_kwh"] = max(0, h["pv_kwh"] - avg_hourly_load)
        h["usable_kwh"] = min(charge_kw, h["surplus_kwh"])

    # Total surplus disponible
    total_surplus = sum(h["usable_kwh"] for h in day_hours)

    # Stratégie : trier les heures par surplus décroissant, prendre jusqu'à atteindre needed_kwh
    sorted_hours = sorted(day_hours, key=lambda h: h["surplus_kwh"], reverse=True)
    selected = []
    accum = 0.0
    for h in sorted_hours:
        if accum >= needed_kwh:
            break
        if h["usable_kwh"] < 0.3:  # ignore les heures quasi-nulles
            continue
        power_used = min(charge_kw, h["usable_kwh"])
        selected.append({**h, "power_kw": round(power_used, 2)})
        accum += power_used

    # Re-tri par heure pour affichage
    selected.sort(key=lambda h: h["hour"])

    # Stratégie grid : si pas assez et allow_grid_import
    grid_used_kwh = 0
    if accum < needed_kwh:
        if allow_grid:
            grid_used_kwh = needed_kwh - accum
        elif accum < min_charge:
            # Pas assez de PV pour le min de charge, et pas d'import autorisé → warning
            grid_used_kwh = min_charge - accum

    # Calcul créneaux contigus pour recommendation simple
    if selected:
        start_hour = min(h["hour"] for h in selected)
        end_hour = max(h["hour"] for h in selected) + 1
        duration_h = len(selected)  # approximatif
        recommended_start = f"{start_hour:02d}:00"
        recommended_end = f"{end_hour:02d}:00"
    else:
        recommended_start = None
        recommended_end = None
        duration_h = 0

    # Expected SoC after
    expected_soc = min(100, current_soc + (accum + grid_used_kwh) / bat_kwh * 100)

    return {
        "enabled": True,
        "target_date": target_date,
        "day_offset": day_offset,
        "config": {
            "battery_capacity_kwh": bat_kwh,
            "charge_power_kw": charge_kw,
            "current_soc": current_soc,
            "target_soc": target_soc,
            "pref_start_hour": pref_start,
            "pref_end_hour": pref_end,
        },
        "needed_kwh": round(needed_kwh, 2),
        "solar_available_kwh": round(total_surplus, 2),
        "selected_hours": selected,
        "recommended_start": recommended_start,
        "recommended_end": recommended_end,
        "duration_hours": duration_h,
        "kwh_from_solar": round(accum, 2),
        "kwh_from_grid": round(grid_used_kwh, 2),
        "expected_soc_after": round(expected_soc, 1),
        "full_day_forecast": day_hours,
        "avg_house_load_kw": round(avg_hourly_load, 2),
        "ok": accum >= needed_kwh or (allow_grid and accum + grid_used_kwh >= needed_kwh),
    }


# ── Export Prometheus / Grafana ──

@app.get("/metrics")
async def prometheus_metrics():
    """
    Endpoint compatible Prometheus scraper et Grafana Agent.
    Format texte plat : `metric_name{labels} value`
    Activable/désactivable via config.integrations.prometheus_enabled (défaut True).
    """
    if _cfg and not _cfg.get().get("integrations", {}).get("prometheus_enabled", True):
        return PlainTextResponse("# Prometheus export désactivé\n",
                                 status_code=404)

    lines = []
    lines.append("# Smart Energy Hub metrics")
    lines.append(f"# Exported at {int(time.time())}\n")

    # Système
    uptime = int(time.time() - _cache["system"]["uptime_start"])
    lines.append(f"# HELP seh_uptime_seconds Uptime of the SEH service")
    lines.append(f"# TYPE seh_uptime_seconds counter")
    lines.append(f"seh_uptime_seconds {uptime}")

    # Victron system
    vic = _cache["victron_system"].get("data")
    if vic:
        lines.append("# HELP seh_pv_power_watts Current PV production (W)")
        lines.append("# TYPE seh_pv_power_watts gauge")
        lines.append(f"seh_pv_power_watts {vic.get('total_pv_power', 0)}")

        lines.append("# HELP seh_load_power_watts Current house consumption (W)")
        lines.append("# TYPE seh_load_power_watts gauge")
        lines.append(f"seh_load_power_watts {vic.get('consumption_power', 0)}")

        lines.append("# HELP seh_grid_power_watts Grid exchange power (positive=import, negative=export)")
        lines.append("# TYPE seh_grid_power_watts gauge")
        lines.append(f"seh_grid_power_watts {vic.get('grid_power', 0)}")

        lines.append("# HELP seh_battery_power_watts Battery power (positive=charge, negative=discharge)")
        lines.append("# TYPE seh_battery_power_watts gauge")
        lines.append(f"seh_battery_power_watts {vic.get('battery_power', 0)}")

        if vic.get("battery_soc") is not None:
            lines.append("# HELP seh_battery_soc_percent Battery state of charge (%)")
            lines.append("# TYPE seh_battery_soc_percent gauge")
            lines.append(f"seh_battery_soc_percent {vic['battery_soc']}")

    # Par MPPT
    sc_units = _cache.get("solarchargers", {}).get("units", {})
    for sid, sc in sc_units.items():
        if not sc.get("online"):
            continue
        labels = f'mppt="{sid}"'
        lines.append(f'seh_mppt_pv_watts{{{labels}}} {sc.get("pv_power", 0)}')
        lines.append(f'seh_mppt_yield_today_kwh{{{labels}}} {sc.get("yield_today_kwh", 0)}')
        lines.append(f'seh_mppt_yield_total_kwh{{{labels}}} {sc.get("yield_user_kwh", 0)}')

    # Par MultiPlus
    mp_units = _cache.get("inverter", {}).get("units", {})
    for mid, mp in mp_units.items():
        if not mp.get("online"):
            continue
        labels = f'multiplus="{mid}"'
        lines.append(f'seh_multiplus_ac_in_watts{{{labels}}} {mp.get("ac_in_power", 0)}')
        lines.append(f'seh_multiplus_ac_out_watts{{{labels}}} {mp.get("ac_out_power", 0)}')

    # Par batterie (tous groupes)
    for gid, grp in _cache.get("bms_groups", {}).items():
        gname = grp.get("name", gid).replace('"', '')
        gtype = grp.get("type", "unknown")
        for bid, bat in grp.get("units", {}).items():
            if not bat.get("online"):
                continue
            labels = f'group="{gname}",type="{gtype}",bat="{bid}"'
            if bat.get("soc") is not None:
                lines.append(f'seh_bms_soc_percent{{{labels}}} {bat["soc"]}')
            if bat.get("voltage") is not None:
                lines.append(f'seh_bms_voltage_volts{{{labels}}} {bat["voltage"]}')
            if bat.get("current") is not None:
                lines.append(f'seh_bms_current_amps{{{labels}}} {bat["current"]}')
            if bat.get("power") is not None:
                lines.append(f'seh_bms_power_watts{{{labels}}} {bat["power"]}')
            if bat.get("temperature") is not None:
                lines.append(f'seh_bms_temperature_celsius{{{labels}}} {bat["temperature"]}')
            if bat.get("cycle_count") is not None:
                lines.append(f'seh_bms_cycles_total{{{labels}}} {bat["cycle_count"]}')
            if bat.get("soh") is not None:
                lines.append(f'seh_bms_soh_percent{{{labels}}} {bat["soh"]}')

    # Totaux journaliers (depuis samples_daily aujourd'hui)
    if _db:
        import datetime as dt
        today_str = dt.datetime.now().strftime("%Y-%m-%d")
        row = _db._conn.execute(
            "SELECT pv_kwh, load_kwh, import_kwh, export_kwh, self_sufficiency "
            "FROM samples_daily WHERE date_str = ?", (today_str,)
        ).fetchone()
        if row:
            lines.append(f"seh_today_pv_kwh {row[0] or 0}")
            lines.append(f"seh_today_load_kwh {row[1] or 0}")
            lines.append(f"seh_today_import_kwh {row[2] or 0}")
            lines.append(f"seh_today_export_kwh {row[3] or 0}")
            lines.append(f"seh_today_self_sufficiency_percent {row[4] or 0}")

    return PlainTextResponse("\n".join(lines) + "\n",
                             media_type="text/plain; version=0.0.4")


@app.get("/api/integrations/influx/status")
async def influx_status():
    """Statut du publisher InfluxDB."""
    if not _influx:
        return {"enabled": False, "message": "Publisher non initialisé"}
    return _influx.get_status()


@app.post("/api/integrations/influx/test")
async def influx_test():
    """Force un push immédiat (sans attendre la boucle) pour tester la config."""
    if not _influx:
        return {"status": "error", "error": "Publisher non initialisé"}
    if not _influx.enabled:
        return {"status": "disabled",
                "message": "Active InfluxDB dans les réglages puis sauvegarde."}
    result = await _influx.push(_cache)
    return result


# ── API Solax (Sprint 8) ──

@app.get("/api/solax")
async def get_solax():
    """État courant de tous les onduleurs Solax configurés."""
    if not _solax:
        return {"enabled": False, "inverters": {}}
    return {
        "enabled": _solax.enabled,
        "poll_interval_seconds": _solax.poll_interval,
        "inverters": _solax.get_cache(),
        "available_plugins": list(__import__("inverters").list_plugins()),
    }


@app.get("/api/solax/scan")
async def solax_scan(inverter_id: str, start: str = "0", count: int = 100,
                     func: str = "HOLDING"):
    """
    Dump brut d'une plage de registres Modbus pour calibration.

    Utiliser :
        GET /api/solax/scan?inverter_id=solax_main&start=0&count=100
        GET /api/solax/scan?inverter_id=solax_main&start=0x46&count=4&func=INPUT

    `start` accepte décimal ou hexa (0x...).
    Retourne les valeurs brutes en U16 et S16 pour chaque adresse, utile pour
    identifier où sont les bonnes données quand on calibre un nouveau modèle.
    """
    if not _solax or not _solax.enabled:
        return {"error": "Solax non activé"}
    # Parser start (hex ou décimal)
    try:
        start_int = int(start, 0) if isinstance(start, str) else int(start)
    except (ValueError, TypeError):
        return {"error": f"start invalide: {start!r}"}
    if isinstance(count, str):
        count = int(count, 0)
    return await _solax.scan(inverter_id, start_int, count, func)


@app.post("/api/solax/write")
async def solax_write(inverter_id: str, key: str, value: float):
    """
    Écriture sécurisée vers un onduleur Solax (whitelist du plugin).

    Sécurité : chaque écriture doit correspondre à une clé dans WRITE_WHITELIST
    du plugin, et la valeur est bornée par WriteSpec.
    """
    if not _solax or not _solax.enabled:
        return {"success": False, "error": "Solax non activé"}
    return await _solax.write(inverter_id, key, value)


@app.get("/api/solax/history")
async def solax_history(range: str = "24h"):
    """
    Historique des données Solax pour le graphique de l'onglet.

    Args:
        range: "24h" (5min sur 24h), "7d" (hourly sur 7j), "30d" (hourly sur 30j),
               "1y" (daily sur 1 an).

    Returns:
        Liste de samples {ts, pv, load, grid, bat, soc} ou similaire selon range.
    """
    if not _db:
        return {"error": "DB non initialisée", "data": []}

    if range == "24h":
        return {"range": range, "data": _db.get_solax_realtime(hours=24)}
    elif range == "7d":
        return {"range": range, "data": _db.get_solax_hourly(days=7)}
    elif range == "30d":
        return {"range": range, "data": _db.get_solax_hourly(days=30)}
    elif range == "1y":
        return {"range": range, "data": _db.get_solax_daily(days=365)}
    else:
        return {"error": f"range invalide: {range}", "data": []}


@app.post("/api/solax/purge_corrupt")
async def solax_purge_corrupt(threshold: float = 50000):
    """
    Purge les samples Solax aberrants (|valeur| > threshold W) dans solax_raw,
    solax_5min, solax_hourly et solax_daily.

    Utile après un bug de lecture du registre feedin_power qui aurait stocké
    des millions de W. Idempotent : peut être lancé plusieurs fois.
    """
    if not _db:
        return {"error": "DB non initialisée"}
    c = _db._conn
    counts = {}
    # Sample bruts
    n_raw = c.execute(
        "DELETE FROM solax_raw WHERE ABS(load_power) > ? OR ABS(grid_power) > ?",
        (threshold, threshold)
    ).rowcount
    # Agrégats 5min
    n_5min = c.execute(
        "DELETE FROM solax_5min WHERE ABS(load_avg) > ? OR ABS(grid_avg) > ? "
        "OR ABS(load_max) > ? OR ABS(grid_max) > ? OR ABS(grid_min) > ?",
        (threshold, threshold, threshold, threshold, threshold)
    ).rowcount
    # Agrégats hourly
    n_hourly = c.execute(
        "DELETE FROM solax_hourly WHERE ABS(load_avg) > ? OR ABS(grid_avg) > ? "
        "OR ABS(load_max) > ? OR ABS(grid_max) > ? OR ABS(grid_min) > ?",
        (threshold, threshold, threshold, threshold, threshold)
    ).rowcount
    # Agrégats daily (en kWh donc threshold différent : 1000 kWh/jour = délire)
    n_daily = c.execute(
        "DELETE FROM solax_daily WHERE ABS(load_kwh) > 1000 OR ABS(import_kwh) > 1000 "
        "OR ABS(export_kwh) > 1000"
    ).rowcount
    c.commit()
    return {
        "status": "ok",
        "threshold_w": threshold,
        "deleted": {
            "solax_raw": n_raw,
            "solax_5min": n_5min,
            "solax_hourly": n_hourly,
            "solax_daily": n_daily,
        }
    }


# ── API Météo (Open-Meteo, gratuit, sans clé) ──

_weather_cache = {"data": None, "ts": 0}

@app.get("/api/weather")
async def get_weather():
    """Météo actuelle et prévisions jour via Open-Meteo."""
    # Utiliser les coordonnées du solar_forecast ou du config
    cfg = _cfg.get() if _cfg else {}
    sf = cfg.get("solar_forecast", {})
    lat = sf.get("latitude", 0)
    lon = sf.get("longitude", 0)
    if not lat or not lon:
        return {"error": "Latitude/longitude non configurées dans Prévisions solaires."}

    # Cache 15 min
    now = time.time()
    if _weather_cache["data"] and (now - _weather_cache["ts"]) < 900:
        return _weather_cache["data"]

    try:
        from urllib.request import urlopen, Request as UrlReq
        import json as _json

        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
            f"weather_code,wind_speed_10m,wind_direction_10m,surface_pressure"
            f"&daily=weather_code,temperature_2m_max,temperature_2m_min,"
            f"sunrise,sunset,uv_index_max,precipitation_sum,wind_speed_10m_max"
            f"&timezone=auto&forecast_days=5"
        )
        loop = asyncio.get_event_loop()
        req = UrlReq(url)
        resp = await loop.run_in_executor(None, lambda: urlopen(req, timeout=10))
        raw = _json.loads(resp.read().decode())

        # Mapper les weather codes en descriptions/icônes
        wmo = {0:'☀️ Dégagé',1:'🌤️ Peu nuageux',2:'⛅ Partiellement nuageux',3:'☁️ Couvert',
               45:'🌫️ Brouillard',48:'🌫️ Givre',51:'🌦️ Bruine légère',53:'🌦️ Bruine',55:'🌧️ Bruine forte',
               61:'🌧️ Pluie légère',63:'🌧️ Pluie',65:'🌧️ Pluie forte',
               71:'🌨️ Neige légère',73:'🌨️ Neige',75:'🌨️ Neige forte',
               80:'🌦️ Averses',81:'🌧️ Averses',82:'⛈️ Averses fortes',
               95:'⛈️ Orage',96:'⛈️ Orage grêle',99:'⛈️ Orage violent'}

        cur = raw.get("current", {})
        daily = raw.get("daily", {})

        # Construire les prévisions jour par jour
        days = []
        d_times = daily.get("time", [])
        for i, dt in enumerate(d_times[:5]):
            wc = daily.get("weather_code", [0])[i] if i < len(daily.get("weather_code", [])) else 0
            days.append({
                "date": dt,
                "icon": wmo.get(wc, '❓').split(' ')[0],
                "desc": wmo.get(wc, 'Inconnu'),
                "temp_max": daily.get("temperature_2m_max", [0])[i],
                "temp_min": daily.get("temperature_2m_min", [0])[i],
                "precipitation": daily.get("precipitation_sum", [0])[i],
                "wind_max": daily.get("wind_speed_10m_max", [0])[i],
                "uv_index": daily.get("uv_index_max", [0])[i],
                "sunrise": daily.get("sunrise", [""])[i][-5:] if daily.get("sunrise") else "",
                "sunset": daily.get("sunset", [""])[i][-5:] if daily.get("sunset") else "",
            })

        wc_cur = cur.get("weather_code", 0)
        result = {
            "current": {
                "temperature": cur.get("temperature_2m"),
                "apparent_temperature": cur.get("apparent_temperature"),
                "humidity": cur.get("relative_humidity_2m"),
                "wind_speed": cur.get("wind_speed_10m"),
                "wind_direction": cur.get("wind_direction_10m"),
                "pressure": cur.get("surface_pressure"),
                "icon": wmo.get(wc_cur, '❓').split(' ')[0],
                "desc": wmo.get(wc_cur, 'Inconnu'),
            },
            "daily": days,
            "location": {"lat": lat, "lon": lon},
        }
        _weather_cache["data"] = result
        _weather_cache["ts"] = now
        return result
    except Exception as e:
        logger.error("Météo Open-Meteo: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


# ── API Alertes ──

@app.post("/api/alerts/test")
async def test_alert():
    """Envoie un message de test Telegram."""
    if not _alerts:
        return {"error": "Module alertes non initialisé"}
    if not _alerts.bot_token:
        return {"error": "Token Telegram non configuré"}
    result = await _alerts.send_test()
    return result


# ── API MQTT ──

@app.get("/api/mqtt/status")
async def mqtt_status():
    """Statut de la connexion MQTT."""
    if not _mqtt:
        return {"enabled": False}
    return {
        "enabled": _mqtt.enabled,
        "connected": _mqtt._connected,
        "broker": _mqtt._config.get("broker", ""),
        "prefix": _mqtt.prefix,
    }


# ── API Fleet (Parc batteries) ──

@app.get("/api/fleet")
async def get_fleet():
    """
    Vue agrégée de toutes les batteries tous groupes confondus.
    Retourne : stats globales du parc + liste plate enrichie de toutes les batteries.
    """
    groups = _cache.get("bms_groups", {})
    fleet = []
    total_nominal_kwh = 0.0
    total_remaining_kwh = 0.0
    total_power_w = 0.0
    sum_soc = 0.0
    count_soc = 0
    online_count = 0
    offline_count = 0
    alarm_count = 0
    charging = 0
    discharging = 0
    idle = 0

    for group_id, group in groups.items():
        group_name = group.get("name", group_id)
        group_type = group.get("type", "unknown")
        units = group.get("units", {})
        for bat_id, bat in units.items():
            online = bool(bat.get("online", False))
            soc = bat.get("soc")
            voltage = bat.get("voltage")
            current = bat.get("current")
            power = bat.get("power")
            if power is None and voltage is not None and current is not None:
                try:
                    power = round(float(voltage) * float(current), 1)
                except Exception:
                    power = None

            # Capacité nominale (kWh) : jkbms fournit nominal_capacity en Ah
            nominal_kwh = None
            remaining_kwh = None
            nominal_ah = bat.get("nominal_capacity")
            remaining_ah = bat.get("remaining_capacity")
            if nominal_ah and voltage:
                try:
                    nominal_kwh = round(float(nominal_ah) * float(voltage) / 1000.0, 2)
                except Exception:
                    pass
            if remaining_ah and voltage:
                try:
                    remaining_kwh = round(float(remaining_ah) * float(voltage) / 1000.0, 2)
                except Exception:
                    pass
            # Fallback pour pylontech (pas de nominal_capacity exposé) : estime 2.4kWh par module
            if nominal_kwh is None and group_type == "pylontech" and online:
                nominal_kwh = 2.4
                if soc is not None:
                    try:
                        remaining_kwh = round(nominal_kwh * float(soc) / 100.0, 2)
                    except Exception:
                        pass

            # Temperature — prendre la plus haute parmi les sondes disponibles
            temps = []
            for k in ("temperature", "temperature2", "temperature3", "temperature4", "temperature5",
                      "temperature_low", "temperature_high", "mos_temperature"):
                v = bat.get(k)
                if v is not None and isinstance(v, (int, float)):
                    temps.append(float(v))
            temp_max = max(temps) if temps else None
            temp_min = min(temps) if temps else None

            # État & alarmes
            base_state = bat.get("base_state")
            if base_state in ("Charge", "Charging"):
                state = "charge"
            elif base_state in ("Dischg", "Discharging"):
                state = "discharge"
            elif base_state == "Idle":
                state = "idle"
            else:
                state = "unknown"

            bat_alarm_count = int(bat.get("alarm_count", 0) or 0)
            has_alarm = bat_alarm_count > 0 or any(
                bat.get(k) == "Alarm"
                for k in ("voltage_state", "current_state", "temperature_state")
            )

            fleet.append({
                "group_id": group_id,
                "group_name": group_name,
                "group_type": group_type,
                "id": bat_id,
                "label": f"{group_name} #{bat_id}",
                "online": online,
                "soc": soc,
                "voltage": voltage,
                "current": current,
                "power": power,
                "temperature": temp_max,
                "temperature_min": temp_min,
                "temperature_max": temp_max,
                "soh": bat.get("soh"),
                "cycle_count": bat.get("cycle_count"),
                "nominal_kwh": nominal_kwh,
                "remaining_kwh": remaining_kwh,
                "state": state,
                "alarm_count": bat_alarm_count,
                "has_alarm": has_alarm,
                "alarms": bat.get("alarms", [])[:5],
            })

            # Stats globales
            if online:
                online_count += 1
                if soc is not None:
                    sum_soc += float(soc)
                    count_soc += 1
                if power is not None:
                    total_power_w += float(power)
                if nominal_kwh:
                    total_nominal_kwh += nominal_kwh
                if remaining_kwh:
                    total_remaining_kwh += remaining_kwh
                if has_alarm:
                    alarm_count += 1
                if state == "charge":
                    charging += 1
                elif state == "discharge":
                    discharging += 1
                elif state == "idle":
                    idle += 1
            else:
                offline_count += 1

    fleet_stats = {
        "total_count": len(fleet),
        "online": online_count,
        "offline": offline_count,
        "alarm_count": alarm_count,
        "charging": charging,
        "discharging": discharging,
        "idle": idle,
        "avg_soc": round(sum_soc / count_soc, 1) if count_soc else None,
        "total_nominal_kwh": round(total_nominal_kwh, 2),
        "total_remaining_kwh": round(total_remaining_kwh, 2),
        "total_power_w": round(total_power_w, 1),
        "group_count": len(groups),
    }

    return {
        "stats": fleet_stats,
        "batteries": fleet,
        "groups": [
            {"id": gid, "name": g.get("name", gid), "type": g.get("type"),
             "count": len(g.get("units", {}))}
            for gid, g in groups.items()
        ],
    }


# ── API ROI / Rentabilité ──

@app.get("/api/roi")
async def get_roi(projection_years: int = 20):
    """
    Calcule la rentabilité de l'installation basée sur l'historique `samples_daily`
    + la config ROI (coût installation, date de mise en service, tarifs import/export).

    - Économies = (import évité = pv_kwh consommée sur place * prix_import)
                  + (export * prix_export)
      Approximation "import évité" = load_kwh - import_kwh
    - Break-even : projection linéaire basée sur la moyenne mensuelle récente
    - Projection 20 ans : dégradation panneaux + inflation tarif EDF
    """
    if not _db or not _cfg:
        return {"error": "DB ou config non initialisée"}

    cfg = _cfg.get()
    roi_cfg = cfg.get("roi", {})
    fin_cfg = cfg.get("finance", {})

    import_price = float(fin_cfg.get("import_price", 0) or 0)
    export_price = float(fin_cfg.get("export_price", 0) or 0)
    installation_cost = float(roi_cfg.get("installation_cost", 0) or 0)
    commissioning_date = str(roi_cfg.get("commissioning_date", "") or "")
    inflation_rate = float(roi_cfg.get("inflation_rate", 3.0) or 0) / 100.0
    panel_deg = float(roi_cfg.get("panel_degradation", 0.5) or 0) / 100.0
    sub_annual = float(roi_cfg.get("subscription_annual", 0) or 0)

    # 1. Agrégation historique — 10 ans max (large pour couvrir toute install)
    daily = _db.get_daily(days=3650)

    total_import_kwh = 0.0
    total_export_kwh = 0.0
    total_pv_kwh = 0.0
    total_load_kwh = 0.0
    total_import_cost = 0.0        # ce qu'on a payé en import
    total_export_revenue = 0.0     # ce qu'on a touché en export
    total_no_solar_cost = 0.0      # ce qu'on aurait payé sans solaire = load * import_price
    by_month = {}                  # {"2024-10": {"import":..., "export":..., "pv":..., "savings":...}}

    for d in daily:
        pv_kwh = float(d.get("pv_kwh", 0) or 0)
        load_kwh = float(d.get("load_kwh", 0) or 0)
        imp_kwh = float(d.get("import_kwh", 0) or 0)
        exp_kwh = float(d.get("export_kwh", 0) or 0)
        total_pv_kwh += pv_kwh
        total_load_kwh += load_kwh
        total_import_kwh += imp_kwh
        total_export_kwh += exp_kwh
        imp_cost = imp_kwh * import_price
        exp_rev = exp_kwh * export_price
        no_solar = load_kwh * import_price
        total_import_cost += imp_cost
        total_export_revenue += exp_rev
        total_no_solar_cost += no_solar

        date_str = d.get("date", "")
        if len(date_str) >= 7:
            month_key = date_str[:7]  # YYYY-MM
            m = by_month.setdefault(month_key, {
                "month": month_key, "pv_kwh": 0, "load_kwh": 0,
                "import_kwh": 0, "export_kwh": 0,
                "import_cost": 0, "export_revenue": 0, "no_solar_cost": 0, "savings": 0,
            })
            m["pv_kwh"] += pv_kwh
            m["load_kwh"] += load_kwh
            m["import_kwh"] += imp_kwh
            m["export_kwh"] += exp_kwh
            m["import_cost"] += imp_cost
            m["export_revenue"] += exp_rev
            m["no_solar_cost"] += no_solar
            m["savings"] += (no_solar - imp_cost) + exp_rev

    # Total économies = (scénario sans solaire) - (import réel) + (revenus export)
    total_savings = (total_no_solar_cost - total_import_cost) + total_export_revenue

    # Tri chronologique des mois + arrondi
    months_list = []
    cumulative = 0.0
    for k in sorted(by_month.keys()):
        m = by_month[k]
        for key in ("pv_kwh", "load_kwh", "import_kwh", "export_kwh",
                    "import_cost", "export_revenue", "no_solar_cost", "savings"):
            m[key] = round(m[key], 2)
        cumulative += m["savings"]
        m["cumulative_savings"] = round(cumulative, 2)
        months_list.append(m)

    # 2. Calcul break-even
    days_of_data = len(daily)
    avg_monthly_savings = 0.0
    if days_of_data > 0 and total_savings > 0:
        avg_monthly_savings = total_savings / days_of_data * 30.44

    break_even = None
    percent_repaid = 0.0
    months_to_breakeven = None
    if installation_cost > 0:
        percent_repaid = round(min(100.0, total_savings / installation_cost * 100.0), 1)
        if avg_monthly_savings > 0 and total_savings < installation_cost:
            remaining = installation_cost - total_savings
            months_to_breakeven = int(remaining / avg_monthly_savings)
            # Projection date
            from datetime import datetime, timedelta
            be_date = datetime.now() + timedelta(days=months_to_breakeven * 30.44)
            break_even = be_date.strftime("%B %Y")

    # 3. Projection 20 ans
    projection = []
    if avg_monthly_savings > 0 and projection_years > 0:
        year_savings = avg_monthly_savings * 12
        cum = total_savings
        for y in range(1, projection_years + 1):
            # Dégradation panneaux : produit moins, économise moins
            # Inflation EDF : le kWh import évité vaut plus cher chaque année
            # Les deux se combinent : net = (1 + inflation) * (1 - deg)^y
            factor = ((1 + inflation_rate) ** y) * ((1 - panel_deg) ** y)
            annual = year_savings * factor
            cum += annual
            projection.append({
                "year": y,
                "annual_savings": round(annual, 2),
                "cumulative_savings": round(cum, 2),
                "roi_percent": round((cum - installation_cost) / installation_cost * 100.0, 1)
                               if installation_cost > 0 else None,
            })

    # 4. ROI annualisé (depuis mise en service)
    roi_annualized = None
    if installation_cost > 0 and commissioning_date:
        try:
            from datetime import datetime
            dt_comm = datetime.strptime(commissioning_date, "%Y-%m-%d")
            years_since = (datetime.now() - dt_comm).days / 365.25
            if years_since > 0:
                roi_annualized = round(total_savings / years_since / installation_cost * 100.0, 2)
        except Exception as e:
            logger.debug(f"ROI annualisé: {e}")

    return {
        "config": {
            "enabled": bool(roi_cfg.get("enabled", False)),
            "installation_cost": installation_cost,
            "commissioning_date": commissioning_date,
            "inflation_rate": inflation_rate * 100,
            "panel_degradation": panel_deg * 100,
            "import_price": import_price,
            "export_price": export_price,
            "currency": fin_cfg.get("currency", "EUR"),
        },
        "summary": {
            "days_of_data": days_of_data,
            "total_pv_kwh": round(total_pv_kwh, 2),
            "total_load_kwh": round(total_load_kwh, 2),
            "total_import_kwh": round(total_import_kwh, 2),
            "total_export_kwh": round(total_export_kwh, 2),
            "total_import_cost": round(total_import_cost, 2),
            "total_export_revenue": round(total_export_revenue, 2),
            "total_no_solar_cost": round(total_no_solar_cost, 2),
            "total_savings": round(total_savings, 2),
            "avg_monthly_savings": round(avg_monthly_savings, 2),
            "percent_repaid": percent_repaid,
            "break_even_date": break_even,
            "months_to_breakeven": months_to_breakeven,
            "roi_annualized_percent": roi_annualized,
        },
        "months": months_list,
        "projection": projection,
    }


# ── API Maintenance DB ──

@app.post("/api/maintenance/recompute")
async def db_recompute():
    """Recompute les agrégats 5min/hourly des dernières 24h (raw samples disponibles).
    Utile après une mise à jour qui a corrigé le calcul import/export."""
    if not _db:
        return {"error": "DB non initialisée"}
    return _db.recompute_recent()


@app.post("/api/maintenance/backfill_export")
async def db_backfill_export(dry_run: bool = False):
    """Estime rétroactivement export_kwh pour les jours passés où il vaut 0.
    Utilise l'équation de conservation PV = Load + Export - Import (+ pertes batterie).
    Utiliser dry_run=true pour prévisualiser sans modifier."""
    if not _db:
        return {"error": "DB non initialisée"}
    return _db.backfill_historical_export(dry_run=dry_run)


@app.post("/api/maintenance/dedupe")
async def db_dedupe(dry_run: bool = False):
    """Dédoublonne samples_5min et samples_hourly + recompute les samples_daily impactés.
    Normalement appliqué automatiquement au démarrage (migration v3), mais utilisable
    manuellement si besoin (par ex. si tu suspectes encore des doublons après un crash)."""
    if not _db:
        return {"error": "DB non initialisée"}
    return _db.dedupe_aggregates(dry_run=dry_run)


@app.post("/api/maintenance/repair_kwh")
async def db_repair_kwh():
    """
    Recalcule les colonnes kWh dans samples_5min depuis les puissances moyennes (pv_avg etc.),
    puis reconstruit hourly et daily. À utiliser si tu vois des valeurs PV/Load à 0
    sur les jours antérieurs au passage en sprint 4, ou des valeurs aberrantes (8000+ kWh).
    Normalement appliqué automatiquement au démarrage (migration v4)."""
    if not _db:
        return {"error": "DB non initialisée"}
    return _db.repair_kwh_columns()


# ── WebSocket ──

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    await ws.send_json({"type": "full_update", "data": {
        "inverter": _cache["inverter"],
        "bms_groups": _cache["bms_groups"],
        "solarchargers": _cache["solarchargers"],
        "victron_system": _cache["victron_system"],
        "solax": _cache.get("solax", {}),
        "finance": _cache["finance"],
        "system": {**_cache["system"], "uptime": int(time.time() - _cache["system"]["uptime_start"])},
    }})
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


# ── Frontend ──
# Sprint 10 : routes explicites pour wizard et login (servies depuis frontend/)
from fastapi.responses import FileResponse

FRONTEND_DIR = os.environ.get("FRONTEND_DIR", "/app/frontend")


@app.get("/setup")
async def serve_setup():
    return FileResponse(os.path.join(FRONTEND_DIR, "setup.html"))


@app.get("/login")
async def serve_login():
    return FileResponse(os.path.join(FRONTEND_DIR, "login.html"))


# Wrapper try/except pour permettre les tests sans /app/frontend
try:
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
except RuntimeError as e:
    logger.warning(f"Frontend statique non monté : {e}. (Normal en mode test)")
