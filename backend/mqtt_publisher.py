"""
Smart Energy Hub — MQTT Publisher
Publie les métriques vers un broker MQTT (Mosquitto, etc.)
Compatible Home Assistant MQTT Auto-Discovery.

Config dans config.json :
  "mqtt": {
    "enabled": true,
    "broker": "192.168.1.10",
    "port": 1883,
    "username": "",
    "password": "",
    "topic_prefix": "smart_energy_hub",
    "ha_discovery": true,
    "ha_discovery_prefix": "homeassistant",
    "publish_interval": 10
  }
"""
import asyncio
import json
import logging
import time

logger = logging.getLogger(__name__)

try:
    from paho.mqtt.client import Client as MQTTClient, MQTTv311
    HAS_PAHO = True
except ImportError:
    HAS_PAHO = False
    logger.debug("paho-mqtt non installé — MQTT désactivé")


class MQTTPublisher:
    """Publie les métriques d'énergie vers un broker MQTT."""

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._client: 'MQTTClient' = None
        self._connected = False
        self._discovery_sent = False
        self._last_publish = 0

    def update_config(self, config: dict):
        self._config = config
        self._discovery_sent = False

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("enabled", False)) and HAS_PAHO

    @property
    def prefix(self) -> str:
        return self._config.get("topic_prefix", "smart_energy_hub")

    @property
    def publish_interval(self) -> int:
        return int(self._config.get("publish_interval", 10))

    def connect(self):
        """Connexion au broker MQTT."""
        if not HAS_PAHO:
            logger.error("paho-mqtt non installé. pip install paho-mqtt")
            return False

        broker = self._config.get("broker", "localhost")
        port = int(self._config.get("port", 1883))
        user = self._config.get("username", "")
        pwd = self._config.get("password", "")

        try:
            self._client = MQTTClient(client_id="smart_energy_hub", protocol=MQTTv311)
            if user:
                self._client.username_pw_set(user, pwd)

            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect

            self._client.connect(broker, port, keepalive=60)
            self._client.loop_start()
            logger.info("MQTT connexion à %s:%d…", broker, port)
            return True
        except Exception as e:
            logger.error("MQTT connexion échouée: %s", e)
            return False

    def disconnect(self):
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None
            self._connected = False

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("MQTT connecté au broker")
            self._connected = True
        else:
            logger.error("MQTT connexion refusée, code=%d", rc)

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        if rc != 0:
            logger.warning("MQTT déconnecté (rc=%d), reconnexion auto…", rc)

    def _publish(self, topic: str, payload, retain: bool = False):
        """Publie un message MQTT."""
        if not self._client or not self._connected:
            return
        try:
            if isinstance(payload, dict):
                payload = json.dumps(payload)
            self._client.publish(topic, payload, retain=retain, qos=0)
        except Exception as e:
            logger.debug("MQTT publish error: %s", e)

    def publish_ha_discovery(self):
        """Envoie les configurations MQTT Auto-Discovery pour Home Assistant."""
        if not self._config.get("ha_discovery", True):
            return
        ha_prefix = self._config.get("ha_discovery_prefix", "homeassistant")
        device = {
            "identifiers": ["smart_energy_hub"],
            "name": "Smart Energy Hub",
            "manufacturer": "DIY",
            "model": "SEH v2",
            "sw_version": "2.0",
        }

        sensors = [
            ("pv_power", "PV Power", "W", "power", "mdi:solar-power", "measurement"),
            ("load_power", "Load Power", "W", "power", "mdi:home-lightning-bolt", "measurement"),
            ("grid_power", "Grid Power", "W", "power", "mdi:transmission-tower", "measurement"),
            ("battery_power", "Battery Power", "W", "power", "mdi:battery-charging", "measurement"),
            ("battery_soc", "Battery SoC", "%", "battery", "mdi:battery", "measurement"),
            ("battery_voltage", "Battery Voltage", "V", "voltage", "mdi:flash", "measurement"),
            ("pv_today_kwh", "PV Today", "kWh", "energy", "mdi:solar-power", "total_increasing"),
        ]

        for sensor_id, name, unit, dev_class, icon, state_class in sensors:
            config_topic = f"{ha_prefix}/sensor/smart_energy_hub/{sensor_id}/config"
            config_payload = {
                "name": name,
                "unique_id": f"seh_{sensor_id}",
                "state_topic": f"{self.prefix}/sensor/{sensor_id}",
                "unit_of_measurement": unit,
                "device_class": dev_class,
                "state_class": state_class,
                "icon": icon,
                "device": device,
            }
            self._publish(config_topic, config_payload, retain=True)

        logger.info("MQTT HA Discovery envoyé (%d sensors)", len(sensors))
        self._discovery_sent = True

    def publish_state(self, cache: dict):
        """Publie l'état actuel du système."""
        now = time.time()
        if now - self._last_publish < self.publish_interval:
            return

        if not self._connected:
            return

        # Envoyer HA Discovery une seule fois
        if not self._discovery_sent:
            self.publish_ha_discovery()

        # ── Extraire les valeurs du cache ──
        vic = cache.get("victron_system", {}).get("data")
        inv_units = cache.get("inverter", {}).get("units", {})
        sc_units = cache.get("solarchargers", {}).get("units", {})

        pv, load, grid, bat_p, soc, bat_v = 0, 0, 0, 0, 0, 0
        pv_today = 0

        if vic and vic.get("online"):
            for sc in sc_units.values():
                if sc.get("online"):
                    pv += sc.get("pv_power", 0) or 0
                    pv_today += sc.get("yield_today_kwh", 0) or 0
            pv += (vic.get("pv_on_output_power", 0) or 0)
            load = vic.get("consumption_power", 0) or 0
            grid = vic.get("grid_power", 0) or 0
            bat_p = vic.get("battery_power", 0) or 0
            soc = vic.get("battery_soc", 0) or 0
            bat_v = vic.get("battery_voltage", 0) or 0
        else:
            for inv in inv_units.values():
                if not inv.get("online"):
                    continue
                pv += float(inv.get("pv_input_power", 0) or 0)
                load += float(inv.get("output_active_power", 0) or 0)
                bv = float(inv.get("battery_voltage", 0) or 0)
                if bv:
                    bat_v = bv
                chg = float(inv.get("battery_charge_current", 0) or 0)
                dis = float(inv.get("battery_discharge_current", 0) or 0)
                bat_p += (chg - dis) * bv
                if inv.get("battery_capacity") is not None:
                    soc = float(inv["battery_capacity"])

        # SoC from BMS if not from inverter
        if soc == 0:
            all_socs = []
            for grp in cache.get("bms_groups", {}).values():
                for unit in grp.get("units", {}).values():
                    if unit.get("online") and unit.get("soc") is not None:
                        all_socs.append(float(unit["soc"]))
            if all_socs:
                soc = sum(all_socs) / len(all_socs)

        # Publier les valeurs individuelles (pour HA)
        prefix = self.prefix
        self._publish(f"{prefix}/sensor/pv_power", str(round(pv)))
        self._publish(f"{prefix}/sensor/load_power", str(round(load)))
        self._publish(f"{prefix}/sensor/grid_power", str(round(grid)))
        self._publish(f"{prefix}/sensor/battery_power", str(round(bat_p)))
        self._publish(f"{prefix}/sensor/battery_soc", str(round(soc)))
        self._publish(f"{prefix}/sensor/battery_voltage", str(round(bat_v, 2)))
        self._publish(f"{prefix}/sensor/pv_today_kwh", str(round(pv_today, 2)))

        # Publier un JSON global
        self._publish(f"{prefix}/state", {
            "pv_power": round(pv),
            "load_power": round(load),
            "grid_power": round(grid),
            "battery_power": round(bat_p),
            "battery_soc": round(soc),
            "battery_voltage": round(bat_v, 2),
            "pv_today_kwh": round(pv_today, 2),
            "timestamp": now,
        })

        # Publier les batteries individuelles
        for grp_key, grp in cache.get("bms_groups", {}).items():
            for uid, unit in grp.get("units", {}).items():
                if not unit.get("online"):
                    continue
                bat_topic = f"{prefix}/battery/{grp_key}/{uid}"
                self._publish(bat_topic, {
                    "soc": unit.get("soc"),
                    "voltage": unit.get("voltage"),
                    "current": unit.get("current"),
                    "power": unit.get("power"),
                    "temperature": unit.get("temperature"),
                    "state": unit.get("base_state"),
                    "cycles": unit.get("cycle_count"),
                })

        self._last_publish = now
