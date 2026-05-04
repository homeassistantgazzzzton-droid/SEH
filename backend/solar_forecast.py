"""
Smart Energy Hub — Solar Forecast
Utilise l'API gratuite Forecast.Solar pour estimer la production PV sur 5 jours.
Endpoint : https://api.forecast.solar/estimate/watthours/period/{lat}/{lon}/{dec}/{az}/{kwp}

Support multi-plans (ex: 2 orientations de toiture différentes).

Config dans config.json :
  "solar_forecast": {
    "enabled": true,
    "latitude": 43.32,
    "longitude": -0.37,
    "planes": [
      {"name": "Toit Sud", "declination": 30, "azimuth": 0, "kwp": 6.0},
      {"name": "Toit Ouest", "declination": 30, "azimuth": 90, "kwp": 4.5}
    ],
    "update_interval_minutes": 60
  }

Azimut Forecast.Solar : 0=Sud, -90=Est, 90=Ouest, 180=Nord
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError

logger = logging.getLogger(__name__)


class SolarForecast:
    """Client Forecast.Solar pour prévisions de production PV."""

    BASE_URL = "https://api.forecast.solar"

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._cache = None
        self._last_fetch = 0

    def update_config(self, config: dict):
        self._config = config
        self._cache = None
        self._last_fetch = 0

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("enabled", False))

    @property
    def update_interval(self) -> int:
        return int(self._config.get("update_interval_minutes", 60)) * 60

    async def fetch_forecast(self) -> dict:
        """Récupère les prévisions pour tous les plans configurés."""
        if not self.enabled:
            return {"error": "Prévisions solaires désactivées"}

        lat = self._config.get("latitude")
        lon = self._config.get("longitude")
        planes = self._config.get("planes", [])

        if not lat or not lon:
            return {"error": "Latitude/longitude non configurées"}
        if not planes:
            return {"error": "Aucun plan PV configuré"}

        # Vérifier le cache
        now = time.time()
        if self._cache and (now - self._last_fetch) < self.update_interval:
            return self._cache

        # Agréger les résultats de tous les plans
        combined_wh = {}  # "YYYY-MM-DD HH:MM:SS" → Wh total
        combined_daily = {}  # "YYYY-MM-DD" → Wh total
        plane_results = []

        for plane in planes:
            dec = plane.get("declination", 30)
            az = plane.get("azimuth", 0)
            kwp = plane.get("kwp", 1.0)
            name = plane.get("name", f"{dec}°/{az}°/{kwp}kWp")

            try:
                # Endpoint watthours/period = Wh produits dans chaque période
                url = f"{self.BASE_URL}/estimate/watthours/period/{lat}/{lon}/{dec}/{az}/{kwp}"
                logger.info("Forecast.Solar: fetching %s (%s)", name, url)

                loop = asyncio.get_event_loop()
                req = Request(url)
                req.add_header("Accept", "application/json")
                resp = await loop.run_in_executor(
                    None, lambda: urlopen(req, timeout=15)
                )
                data = json.loads(resp.read().decode())

                if data.get("result"):
                    wh_data = data["result"]
                    daily_total = {}

                    for ts_str, wh in wh_data.items():
                        # Accumuler les Wh par période
                        combined_wh[ts_str] = combined_wh.get(ts_str, 0) + wh
                        # Accumuler par jour
                        day = ts_str[:10]
                        daily_total[day] = daily_total.get(day, 0) + wh

                    for day, total in daily_total.items():
                        combined_daily[day] = combined_daily.get(day, 0) + total

                    plane_results.append({
                        "name": name,
                        "kwp": kwp,
                        "daily_kwh": {d: round(v / 1000, 1) for d, v in daily_total.items()},
                        "ok": True,
                    })
                    logger.info("Forecast.Solar: %s → %d périodes, %d jours",
                                name, len(wh_data), len(daily_total))
                else:
                    plane_results.append({"name": name, "ok": False, "error": "Pas de données"})

            except HTTPError as e:
                logger.error("Forecast.Solar HTTP %d pour %s", e.code, name)
                plane_results.append({"name": name, "ok": False, "error": f"HTTP {e.code}"})
            except Exception as e:
                logger.error("Forecast.Solar error %s: %s", name, e)
                plane_results.append({"name": name, "ok": False, "error": str(e)})

        # Facteur de correction global (ombrages, salissure, micro-onduleurs perdants...)
        # loss_percent : 0 = aucune perte (production brute Forecast.Solar)
        #               20 = on retire 20% (production réelle ~80% du modèle théorique)
        loss_pct = float(self._config.get("loss_percent", 0) or 0)
        loss_factor = max(0.0, min(1.0, 1.0 - loss_pct / 100.0))

        # Construire le résultat
        # Hourly data triées
        hourly = []
        for ts_str in sorted(combined_wh.keys()):
            wh = combined_wh[ts_str] * loss_factor
            try:
                dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                hourly.append({
                    "datetime": ts_str,
                    "ts": dt.timestamp(),
                    "wh": wh,
                    "kw": round(wh / 1000, 2),  # kW moyen sur la période (~1h)
                })
            except ValueError:
                pass

        # Daily totals triés
        daily = []
        for day_str in sorted(combined_daily.keys()):
            wh = combined_daily[day_str] * loss_factor
            daily.append({
                "date": day_str,
                "kwh": round(wh / 1000, 1),
                "label": _day_label(day_str),
            })

        result = {
            "hourly": hourly,
            "daily": daily[:5],  # 5 jours max
            "planes": plane_results,
            "total_kwp": sum(p.get("kwp", 0) for p in planes),
            "loss_percent": loss_pct,  # info pour le frontend
            "last_update": time.time(),
            "location": {"lat": lat, "lon": lon},
        }

        self._cache = result
        self._last_fetch = now
        return result

    def get_cached(self) -> dict:
        """Retourne les données cachées ou vide."""
        return self._cache or {"hourly": [], "daily": [], "planes": []}


def _day_label(date_str: str) -> str:
    """Convertit une date en label J, J+1, J+2..."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = datetime.now().date()
        delta = (dt - today).days
        if delta == 0:
            return "Aujourd'hui"
        elif delta == 1:
            return "Demain"
        else:
            return f"J+{delta}"
    except ValueError:
        return date_str
