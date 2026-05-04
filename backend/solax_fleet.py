"""
Smart Energy Hub — Solax Fleet
==============================

Couche d'orchestration pour 1 ou plusieurs onduleurs Solax connectés en TCP.
Suit le même pattern que victron.py / voltronic.py : poll régulier qui alimente
le cache global.
"""

import asyncio
import logging
import time
from typing import Optional

from inverters import get_plugin
from inverters.modbus_client import ModbusInverterClient

logger = logging.getLogger(__name__)


class SolaxFleet:
    """
    Gère 1 ou plusieurs onduleurs Solax. Le cache est mis à jour à chaque poll.

    Pour cohabiter avec Victron/Voltronic existants, on alimente _cache["solax"]
    sans toucher aux autres clés.
    """

    def __init__(self, config: dict):
        """
        Args:
            config: dict avec :
                enabled: bool
                inverters: list de {plugin_name, host, port, unit_id, ...}
                poll_interval_seconds: int (défaut 10)
        """
        self._config = config or {}
        self._clients: dict = {}  # {inverter_id: ModbusInverterClient}
        self._cache: dict = {}    # {inverter_id: {data, last_update, online}}
        self._stop = False

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("enabled", False))

    @property
    def poll_interval(self) -> int:
        return int(self._config.get("poll_interval_seconds", 10))

    def update_config(self, config: dict):
        """Met à jour la config et recrée les clients."""
        self._config = config
        # Reset clients - seront recréés au prochain poll
        for cid, client in self._clients.items():
            asyncio.create_task(client.disconnect())
        self._clients = {}

    async def _ensure_clients(self):
        """Crée/recrée les clients Modbus selon la config."""
        inverters = self._config.get("inverters", [])
        for inv_cfg in inverters:
            inv_id = inv_cfg.get("id") or inv_cfg.get("host")
            if not inv_id or inv_id in self._clients:
                continue
            plugin_name = inv_cfg.get("plugin_name", "solax_x1_hybrid_gen4")
            plugin_cls = get_plugin(plugin_name)
            if not plugin_cls:
                logger.warning(f"Plugin Solax '{plugin_name}' inconnu pour {inv_id}")
                continue
            try:
                plugin = plugin_cls(inv_cfg)
                client = ModbusInverterClient(plugin)
                self._clients[inv_id] = client
                logger.info(f"Solax: client créé pour {inv_id} ({plugin_name})")
            except Exception as e:
                logger.warning(f"Solax: erreur création client {inv_id}: {e}")

    async def poll_once(self):
        """Lit l'état de tous les onduleurs configurés et met à jour le cache."""
        if not self.enabled:
            return

        await self._ensure_clients()

        for inv_id, client in self._clients.items():
            try:
                if not client.is_connected:
                    await client.connect()

                raw = await client.read_status()
                if raw is None:
                    self._cache[inv_id] = {
                        "online": False,
                        "last_update": time.time(),
                        "error": "Read failed",
                    }
                    continue

                status = client.plugin.parse_status(raw)
                status.last_update = time.time()

                self._cache[inv_id] = {
                    "online": status.online,
                    "last_update": status.last_update,
                    "data": {
                        "serial": status.serial,
                        "model": status.model,
                        "firmware": status.firmware,
                        "inverter_type": status.inverter_type,
                        "pv_power": status.pv_power,
                        "pv1_power": status.pv1_power,
                        "pv2_power": status.pv2_power,
                        "pv1_voltage": status.pv1_voltage,
                        "pv2_voltage": status.pv2_voltage,
                        "pv1_current": status.pv1_current,
                        "pv2_current": status.pv2_current,
                        "grid_power": status.grid_power,
                        "grid_voltage": status.grid_voltage,
                        "grid_frequency": status.grid_frequency,
                        "load_power": status.load_power,
                        "battery_power": status.battery_power,
                        "battery_soc": status.battery_soc,
                        "battery_voltage": status.battery_voltage,
                        "battery_current": status.battery_current,
                        "battery_temperature": status.battery_temperature,
                        "yield_today": status.yield_today,
                        "yield_total": status.yield_total,
                        "import_today": status.import_today,
                        "export_today": status.export_today,
                        "import_total": status.import_total,
                        "export_total": status.export_total,
                        "inverter_status": status.inverter_status,
                        "inverter_temperature": status.inverter_temperature,
                    },
                    "raw": status.raw,
                }
            except Exception as e:
                logger.warning(f"Solax poll error {inv_id}: {e}")
                self._cache[inv_id] = {
                    "online": False,
                    "last_update": time.time(),
                    "error": str(e),
                }

    def get_cache(self) -> dict:
        """Retourne l'état actuel du cache (pour main.py)."""
        return self._cache

    async def write(self, inv_id: str, key: str, value: float) -> dict:
        """Écrit dans un onduleur (whitelist plugin)."""
        client = self._clients.get(inv_id)
        if not client:
            return {"success": False, "error": f"Onduleur '{inv_id}' inconnu"}
        return await client.write_value(key, value)

    async def scan(self, inv_id: str, start: int, count: int,
                   func: str = "HOLDING") -> dict:
        """Dump brut des registres pour calibration."""
        from inverters.base import RegisterFunc
        client = self._clients.get(inv_id)
        if not client:
            return {"error": f"Onduleur '{inv_id}' inconnu"}
        f = RegisterFunc.HOLDING if func.upper() == "HOLDING" else RegisterFunc.INPUT
        return await client.scan_registers(start, count, f)

    async def stop(self):
        """Ferme proprement toutes les connexions."""
        self._stop = True
        for client in self._clients.values():
            try:
                await client.disconnect()
            except Exception:
                pass
