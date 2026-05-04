"""
Smart Energy Hub — InfluxDB v2 Publisher
Push des métriques en line protocol via HTTP.

Config dans config.json :
  "integrations": {
    "influxdb_enabled": true,
    "influxdb_url": "http://192.168.1.10:8086",
    "influxdb_org": "home",
    "influxdb_bucket": "solar",
    "influxdb_token": "your-write-token",
    "influxdb_push_interval_seconds": 30
  }

Format line protocol :
  measurement,tag1=v1,tag2=v2 field1=v1,field2=v2 timestamp_ns
"""
import asyncio
import logging
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

logger = logging.getLogger(__name__)


class InfluxPublisher:
    def __init__(self, config: dict = None):
        self._config = config or {}
        self._last_push = 0
        self._failed_count = 0
        self._success_count = 0
        self._last_error = None

    def update_config(self, config: dict):
        self._config = config

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("influxdb_enabled", False)
                    and self._config.get("influxdb_url")
                    and self._config.get("influxdb_token"))

    @property
    def interval(self) -> int:
        return int(self._config.get("influxdb_push_interval_seconds", 30))

    def _format_lines(self, cache: dict) -> list:
        """Construit les lignes line-protocol à partir du cache."""
        ts_ns = int(time.time() * 1_000_000_000)
        lines = []

        # Système
        vic = cache.get("victron_system", {}).get("data")
        if vic:
            fields = []
            for k, metric in [
                ("total_pv_power", "pv_power"),
                ("consumption_power", "load_power"),
                ("grid_power", "grid_power"),
                ("battery_power", "battery_power"),
                ("battery_soc", "battery_soc"),
            ]:
                v = vic.get(k)
                if v is not None:
                    fields.append(f"{metric}={float(v)}")
            if fields:
                lines.append(f"seh_system {','.join(fields)} {ts_ns}")

        # MPPT
        for sid, sc in cache.get("solarchargers", {}).get("units", {}).items():
            if not sc.get("online"):
                continue
            fields = []
            for k, metric in [
                ("pv_power", "pv_power"),
                ("yield_today_kwh", "yield_today_kwh"),
                ("yield_user_kwh", "yield_total_kwh"),
            ]:
                v = sc.get(k)
                if v is not None:
                    fields.append(f"{metric}={float(v)}")
            if fields:
                lines.append(f"seh_mppt,mppt={sid} {','.join(fields)} {ts_ns}")

        # MultiPlus
        for mid, mp in cache.get("inverter", {}).get("units", {}).items():
            if not mp.get("online"):
                continue
            fields = []
            for k, metric in [
                ("ac_in_power", "ac_in_power"),
                ("ac_out_power", "ac_out_power"),
                ("battery_voltage", "battery_voltage"),
            ]:
                v = mp.get(k)
                if v is not None:
                    fields.append(f"{metric}={float(v)}")
            if fields:
                lines.append(f"seh_multiplus,multiplus={mid} {','.join(fields)} {ts_ns}")

        # BMS batteries
        for gid, grp in cache.get("bms_groups", {}).items():
            gtype = grp.get("type", "unknown")
            gname = grp.get("name", gid).replace(" ", "_").replace(",", "_")
            for bid, bat in grp.get("units", {}).items():
                if not bat.get("online"):
                    continue
                fields = []
                for k, metric in [
                    ("soc", "soc"),
                    ("voltage", "voltage"),
                    ("current", "current"),
                    ("power", "power"),
                    ("temperature", "temperature"),
                    ("cycle_count", "cycles"),
                    ("soh", "soh"),
                ]:
                    v = bat.get(k)
                    if v is not None:
                        fields.append(f"{metric}={float(v)}")
                if fields:
                    tags = f"group={gname},type={gtype},bat={bid}"
                    lines.append(f"seh_bms,{tags} {','.join(fields)} {ts_ns}")

        return lines

    async def push(self, cache: dict) -> dict:
        """Envoie le snapshot courant vers InfluxDB."""
        if not self.enabled:
            return {"status": "disabled"}

        lines = self._format_lines(cache)
        if not lines:
            return {"status": "empty"}

        body = "\n".join(lines).encode("utf-8")

        url = (self._config["influxdb_url"].rstrip("/")
               + "/api/v2/write"
               + f"?org={self._config.get('influxdb_org', '')}"
               + f"&bucket={self._config.get('influxdb_bucket', 'solar')}"
               + "&precision=ns")

        req = Request(url, data=body, method="POST")
        req.add_header("Authorization", f"Token {self._config['influxdb_token']}")
        req.add_header("Content-Type", "text/plain; charset=utf-8")

        try:
            # urlopen est bloquant → exécute dans un thread pour pas bloquer l'event loop
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: urlopen(req, timeout=5).read()
            )
            self._success_count += 1
            self._last_error = None
            self._last_push = time.time()
            return {"status": "ok", "lines": len(lines)}
        except HTTPError as e:
            self._failed_count += 1
            msg = f"HTTP {e.code}: {e.reason}"
            self._last_error = msg
            logger.warning(f"InfluxDB push failed: {msg}")
            return {"status": "error", "error": msg}
        except URLError as e:
            self._failed_count += 1
            msg = f"Connection error: {e.reason}"
            self._last_error = msg
            logger.warning(f"InfluxDB push failed: {msg}")
            return {"status": "error", "error": msg}
        except Exception as e:
            self._failed_count += 1
            self._last_error = str(e)
            logger.warning(f"InfluxDB push unexpected error: {e}")
            return {"status": "error", "error": str(e)}

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "url": self._config.get("influxdb_url", ""),
            "bucket": self._config.get("influxdb_bucket", ""),
            "success_count": self._success_count,
            "failed_count": self._failed_count,
            "last_error": self._last_error,
            "last_push": self._last_push,
            "last_push_ago_s": int(time.time() - self._last_push) if self._last_push else None,
        }
