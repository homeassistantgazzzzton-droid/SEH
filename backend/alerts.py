"""
Smart Energy Hub — Alertes & Notifications Telegram
Envoie des alertes automatiques via Telegram Bot quand :
  - SoC batterie descend sous un seuil
  - SoC batterie remonte (recovery)
  - Surcharge onduleur (load > seuil %)
  - Déconnexion onduleur ou BMS
  - Alarmes BMS actives
  - Production PV nulle en journée (optionnel)

Config dans config.json :
  "alerts": {
    "enabled": true,
    "telegram_bot_token": "123456:ABC-DEF...",
    "telegram_chat_id": "987654321",
    "soc_low_threshold": 20,
    "soc_critical_threshold": 10,
    "load_high_threshold": 85,
    "cooldown_minutes": 15,
    "daily_summary": true,
    "daily_summary_hour": 21
  }
"""
import asyncio
import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)

try:
    from urllib.request import urlopen, Request
    from urllib.parse import urlencode, quote
    HAS_URLLIB = True
except ImportError:
    HAS_URLLIB = False


class AlertManager:
    """Gestionnaire d'alertes avec cooldown et notifications Telegram."""

    # Types d'alertes et leur cooldown par défaut
    ALERT_TYPES = {
        "soc_low":        {"emoji": "🪫", "severity": "warning"},
        "soc_critical":   {"emoji": "🚨", "severity": "critical"},
        "soc_recovery":   {"emoji": "🔋", "severity": "info"},
        "overload":       {"emoji": "⚠️", "severity": "warning"},
        "inverter_offline": {"emoji": "❌", "severity": "critical"},
        "bms_offline":    {"emoji": "❌", "severity": "critical"},
        "bms_alarm":      {"emoji": "🚨", "severity": "critical"},
        "bms_recovery":   {"emoji": "✅", "severity": "info"},
        "inverter_recovery": {"emoji": "✅", "severity": "info"},
        "daily_summary":  {"emoji": "📊", "severity": "info"},
        "pv_low":         {"emoji": "☁️", "severity": "warning"},
        "cycles_warning": {"emoji": "🔄", "severity": "warning"},
        "source_stale":   {"emoji": "📡", "severity": "warning"},
        "weekly_summary": {"emoji": "📅", "severity": "info"},
    }

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._last_alert: dict[str, float] = {}  # type_key → timestamp
        self._active_alerts: dict[str, str] = {}  # type_key → message
        self._daily_stats = {
            "alerts_sent": 0,
            "pv_peak_w": 0,
            "load_peak_w": 0,
            "min_soc": 100,
            "max_soc": 0,
            "pv_wh_acc": 0,
            "load_wh_acc": 0,
            "import_wh_acc": 0,
            "export_wh_acc": 0,
            "samples": 0,
        }
        self._last_summary_date = ""
        self._last_weekly_date = ""
        self._inverter_was_online = False
        self._bms_was_online: dict[str, bool] = {}
        # Historique par heure pour comparaison PV low (moyenne glissante 7j)
        self._pv_history_hourly: dict[str, list] = {}  # "HH" → [pv_w, ...]
        # Dernière mise à jour par source pour détecter silence prolongé
        self._source_last_seen: dict[str, float] = {}
        # Référence cycles batterie pour alerter une seule fois
        self._bat_cycles_warned: set = set()  # {"group_id#bat_id", ...}

    def update_config(self, config: dict):
        self._config = config

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("enabled", False))

    @property
    def bot_token(self) -> str:
        return self._config.get("telegram_bot_token", "")

    @property
    def chat_id(self) -> str:
        return str(self._config.get("telegram_chat_id", ""))

    @property
    def cooldown(self) -> int:
        return int(self._config.get("cooldown_minutes", 15)) * 60

    def _can_send(self, alert_key: str) -> bool:
        """Vérifie le cooldown pour éviter le spam."""
        last = self._last_alert.get(alert_key, 0)
        return (time.time() - last) > self.cooldown

    def _mark_sent(self, alert_key: str):
        self._last_alert[alert_key] = time.time()

    async def send_telegram(self, message: str) -> bool:
        """Envoie un message via l'API Telegram Bot."""
        if not self.bot_token or not self.chat_id:
            logger.warning("Alertes Telegram: token ou chat_id manquant")
            return False
        if not HAS_URLLIB:
            logger.error("urllib non disponible")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        data = urlencode({
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()

        try:
            req = Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(None, lambda: urlopen(req, timeout=10))
            if resp.status == 200:
                logger.info("Telegram alert envoyée")
                self._daily_stats["alerts_sent"] += 1
                return True
            else:
                logger.warning("Telegram API status %d", resp.status)
                return False
        except Exception as e:
            logger.error("Telegram send error: %s", e)
            return False

    async def check_and_alert(self, cache: dict):
        """Vérifie l'état du système et envoie les alertes nécessaires."""
        if not self.enabled or not self.bot_token:
            return

        now = time.time()
        soc_low = int(self._config.get("soc_low_threshold", 20))
        soc_crit = int(self._config.get("soc_critical_threshold", 10))
        load_high = int(self._config.get("load_high_threshold", 85))

        # ── SoC Batterie ──
        soc = None
        vic = cache.get("victron_system", {}).get("data")
        if vic and vic.get("battery_soc") is not None:
            soc = vic["battery_soc"]
        else:
            # Moyenne de tous les BMS
            all_socs = []
            for grp in cache.get("bms_groups", {}).values():
                for unit in grp.get("units", {}).values():
                    if unit.get("online") and unit.get("soc") is not None:
                        all_socs.append(float(unit["soc"]))
            if all_socs:
                soc = sum(all_socs) / len(all_socs)

        if soc is not None:
            self._daily_stats["min_soc"] = min(self._daily_stats["min_soc"], soc)
            self._daily_stats["max_soc"] = max(self._daily_stats["max_soc"], soc)

            if soc <= soc_crit and self._can_send("soc_critical"):
                await self.send_telegram(
                    f"🚨 <b>SoC CRITIQUE : {soc:.0f}%</b>\n"
                    f"Le niveau de batterie est dangereusement bas !"
                )
                self._mark_sent("soc_critical")
                self._active_alerts["soc"] = "critical"

            elif soc <= soc_low and self._can_send("soc_low"):
                if self._active_alerts.get("soc") != "critical":
                    await self.send_telegram(
                        f"🪫 <b>SoC bas : {soc:.0f}%</b>\n"
                        f"Seuil d'alerte : {soc_low}%"
                    )
                    self._mark_sent("soc_low")
                    self._active_alerts["soc"] = "low"

            elif soc > soc_low + 5 and "soc" in self._active_alerts:
                if self._can_send("soc_recovery"):
                    await self.send_telegram(
                        f"🔋 <b>SoC rétabli : {soc:.0f}%</b>\n"
                        f"La batterie recharge."
                    )
                    self._mark_sent("soc_recovery")
                    del self._active_alerts["soc"]

        # ── Surcharge onduleur ──
        for uid, inv in cache.get("inverter", {}).get("units", {}).items():
            load_pct = inv.get("output_load_percent")
            if load_pct is not None and load_pct > load_high:
                key = f"overload_{uid}"
                if self._can_send(key):
                    await self.send_telegram(
                        f"⚠️ <b>Surcharge onduleur #{uid} : {load_pct}%</b>\n"
                        f"Seuil : {load_high}%"
                    )
                    self._mark_sent(key)

        # ── Déconnexion onduleur ──
        inv_units = cache.get("inverter", {}).get("units", {})
        inv_online = any(u.get("online") for u in inv_units.values()) if inv_units else False
        inv_error = cache.get("inverter", {}).get("error")

        if self._inverter_was_online and not inv_online and inv_error:
            if self._can_send("inverter_offline"):
                await self.send_telegram(
                    f"❌ <b>Onduleur déconnecté</b>\n"
                    f"Erreur : {inv_error}"
                )
                self._mark_sent("inverter_offline")
                self._active_alerts["inverter"] = "offline"

        elif not self._inverter_was_online and inv_online and "inverter" in self._active_alerts:
            if self._can_send("inverter_recovery"):
                await self.send_telegram(
                    f"✅ <b>Onduleur reconnecté</b>"
                )
                self._mark_sent("inverter_recovery")
                del self._active_alerts["inverter"]

        self._inverter_was_online = inv_online

        # ── Alarmes BMS ──
        for grp_key, grp in cache.get("bms_groups", {}).items():
            for uid, unit in grp.get("units", {}).items():
                bms_key = f"{grp_key}_{uid}"

                # Déconnexion BMS
                was_online = self._bms_was_online.get(bms_key, False)
                is_online = unit.get("online", False)

                if was_online and not is_online:
                    if self._can_send(f"bms_offline_{bms_key}"):
                        await self.send_telegram(
                            f"❌ <b>BMS #{uid} ({grp.get('name', grp_key)}) déconnecté</b>"
                        )
                        self._mark_sent(f"bms_offline_{bms_key}")

                elif not was_online and is_online:
                    if f"bms_offline_{bms_key}" in self._last_alert:
                        if self._can_send(f"bms_recovery_{bms_key}"):
                            await self.send_telegram(
                                f"✅ <b>BMS #{uid} ({grp.get('name', grp_key)}) reconnecté</b>"
                            )
                            self._mark_sent(f"bms_recovery_{bms_key}")

                self._bms_was_online[bms_key] = is_online

                # Alarmes actives
                alarms = unit.get("alarms", [])
                if alarms and is_online:
                    alarm_key = f"bms_alarm_{bms_key}"
                    if self._can_send(alarm_key):
                        alarm_list = "\n".join(f"  • {a}" for a in alarms[:5])
                        await self.send_telegram(
                            f"🚨 <b>Alarme BMS #{uid} ({grp.get('name', grp_key)})</b>\n"
                            f"{alarm_list}"
                        )
                        self._mark_sent(alarm_key)

        # ── Stats PV/Load/Grid pour le résumé quotidien ──
        grid_power = 0
        if vic:
            pv = vic.get("total_pv_power", 0) or 0
            load = vic.get("consumption_power", 0) or 0
            grid_power = vic.get("grid_power", 0) or 0
        else:
            pv = sum(float(u.get("pv_input_power", 0) or 0)
                     for u in inv_units.values() if u.get("online"))
            load = sum(float(u.get("output_active_power", 0) or 0)
                       for u in inv_units.values() if u.get("online"))
        self._daily_stats["pv_peak_w"] = max(self._daily_stats["pv_peak_w"], pv)
        self._daily_stats["load_peak_w"] = max(self._daily_stats["load_peak_w"], load)
        # Accumuler l'énergie (Wh) — chaque sample = poll_interval secondes
        poll_h = 10 / 3600  # approximation ~10s par sample
        self._daily_stats["pv_wh_acc"] += pv * poll_h
        self._daily_stats["load_wh_acc"] += load * poll_h
        if grid_power > 0:
            self._daily_stats["import_wh_acc"] += grid_power * poll_h
        elif grid_power < 0:
            self._daily_stats["export_wh_acc"] += abs(grid_power) * poll_h
        self._daily_stats["samples"] += 1

        # ── PV bas anormal (comparaison moyenne 7j même heure) ──
        if self._config.get("pv_low_alert", True):
            try:
                now_dt = datetime.now()
                hour_key = f"{now_dt.hour:02d}"
                # On stocke les valeurs PV par heure, glissant sur 7j (168 samples approximatifs par heure)
                hist = self._pv_history_hourly.setdefault(hour_key, [])
                hist.append(pv)
                if len(hist) > 168:  # rolling window
                    hist.pop(0)
                # Vérifier seulement en journée (8h-18h) et avec assez d'historique
                if 8 <= now_dt.hour <= 18 and len(hist) >= 24:
                    avg_hist = sum(hist[:-1]) / (len(hist) - 1) if len(hist) > 1 else 0
                    threshold_pct = float(self._config.get("pv_low_threshold_pct", 40))
                    # Alerte si PV actuel < X% de la moyenne historique ET moyenne > 500W (évite bruit nuit/nuages lourds)
                    if avg_hist > 500 and pv < avg_hist * threshold_pct / 100:
                        if self._can_send("pv_low"):
                            await self.send_telegram(
                                f"☁️ <b>PV anormalement bas</b>\n"
                                f"Actuel : {pv:.0f} W\n"
                                f"Moyenne {hour_key}h sur 7j : {avg_hist:.0f} W\n"
                                f"Ratio : {pv/avg_hist*100:.0f}% du normal"
                            )
                            self._mark_sent("pv_low")
            except Exception as e:
                logger.debug(f"PV low check error: {e}")

        # ── Source stale (pas de mise à jour depuis > N min) ──
        if self._config.get("source_stale_alert", True):
            stale_minutes = int(self._config.get("source_stale_minutes", 15))
            stale_seconds = stale_minutes * 60
            # Victron system
            vic_cache = cache.get("victron_system", {})
            if vic_cache.get("last_update"):
                age = now - vic_cache["last_update"]
                if age > stale_seconds and self._can_send(f"source_stale_victron"):
                    await self.send_telegram(
                        f"📡 <b>Victron silencieux</b>\n"
                        f"Pas de données depuis {int(age/60)} min"
                    )
                    self._mark_sent("source_stale_victron")
            # Groupes BMS
            for gid, grp in cache.get("bms_groups", {}).items():
                if grp.get("last_update"):
                    age = now - grp["last_update"]
                    if age > stale_seconds and self._can_send(f"source_stale_{gid}"):
                        await self.send_telegram(
                            f"📡 <b>{grp.get('name', gid)} silencieux</b>\n"
                            f"Pas de données depuis {int(age/60)} min"
                        )
                        self._mark_sent(f"source_stale_{gid}")

        # ── Cycles batterie dépassés ──
        if self._config.get("cycles_alert", True):
            cycles_threshold = int(self._config.get("cycles_threshold", 4000))
            for gid, grp in cache.get("bms_groups", {}).items():
                for bid, bat in grp.get("units", {}).items():
                    if not bat.get("online"):
                        continue
                    cycles = bat.get("cycle_count")
                    if cycles and cycles >= cycles_threshold:
                        bat_key = f"{gid}#{bid}"
                        if bat_key not in self._bat_cycles_warned and self._can_send(f"cycles_{bat_key}"):
                            await self.send_telegram(
                                f"🔄 <b>Cycles batterie élevés</b>\n"
                                f"{grp.get('name', gid)} #{bid} : <b>{cycles} cycles</b>\n"
                                f"Seuil : {cycles_threshold}\n"
                                f"Pense à surveiller sa dégradation."
                            )
                            self._mark_sent(f"cycles_{bat_key}")
                            self._bat_cycles_warned.add(bat_key)

        # ── Résumé quotidien ──
        if self._config.get("daily_summary", True):
            summary_hour = int(self._config.get("daily_summary_hour", 21))
            now_dt = datetime.now()
            today_str = now_dt.strftime("%Y-%m-%d")
            if now_dt.hour == summary_hour and self._last_summary_date != today_str:
                await self._send_daily_summary(cache)
                self._last_summary_date = today_str

        # ── Résumé hebdomadaire (samedi soir) ──
        if self._config.get("weekly_summary", True):
            summary_hour = int(self._config.get("weekly_summary_hour", 20))
            now_dt = datetime.now()
            today_str = now_dt.strftime("%Y-%m-%d")
            # Samedi = 5 en weekday()
            if (now_dt.weekday() == 5 and now_dt.hour == summary_hour
                    and self._last_weekly_date != today_str):
                await self._send_weekly_summary()
                self._last_weekly_date = today_str

    async def _send_weekly_summary(self):
        """Envoie le résumé de la semaine en cours (lundi→samedi soir)."""
        try:
            # Accès DB via import local (évite import circulaire au chargement)
            import main as m
            if not m._db:
                return
            daily = m._db.get_daily(days=8)
            if not daily:
                return

            now_dt = datetime.now()
            # Semaine courante = lundi de cette semaine → aujourd'hui
            import datetime as dt
            monday = now_dt - dt.timedelta(days=now_dt.weekday())
            monday_str = monday.strftime("%Y-%m-%d")
            week = [d for d in daily if d["date"] >= monday_str]
            if not week:
                return

            tot_pv = sum(d.get("pv_kwh", 0) or 0 for d in week)
            tot_load = sum(d.get("load_kwh", 0) or 0 for d in week)
            tot_imp = sum(d.get("import_kwh", 0) or 0 for d in week)
            tot_exp = sum(d.get("export_kwh", 0) or 0 for d in week)
            self_suff = ((tot_load - tot_imp) / tot_load * 100) if tot_load > 0 else 0

            # Prix
            import_price = export_price = 0
            cur = "EUR"
            if m._cfg:
                fin = m._cfg.get().get("finance", {})
                import_price = float(fin.get("import_price", 0) or 0)
                export_price = float(fin.get("export_price", 0) or 0)
                cur = fin.get("currency", "EUR")
            sym = {"EUR": "€", "USD": "$"}.get(cur, cur)
            savings = (tot_load * import_price - tot_imp * import_price) + tot_exp * export_price

            best = max(week, key=lambda d: d.get("pv_kwh", 0) or 0)
            worst = min(week, key=lambda d: d.get("pv_kwh", 0) or 0)

            msg = f"📅 <b>Semaine du {monday.strftime('%d/%m')} au {now_dt.strftime('%d/%m')}</b>\n"
            msg += f"{'─' * 28}\n\n"
            msg += f"☀️ <b>Production</b> : {tot_pv:.1f} kWh\n"
            msg += f"🏠 <b>Consommation</b> : {tot_load:.1f} kWh\n"
            msg += f"🔌 <b>Import</b> : {tot_imp:.1f} kWh\n"
            msg += f"⚡ <b>Export</b> : {tot_exp:.1f} kWh\n\n"
            msg += f"📈 <b>Autosuffisance</b> : {self_suff:.0f}%\n"
            if import_price > 0:
                msg += f"💰 <b>Économies</b> : {savings:.2f} {sym}\n\n"
            msg += f"🏆 <b>Meilleur jour</b> : {best['date']} ({best.get('pv_kwh', 0):.1f} kWh)\n"
            msg += f"☁️ <b>Plus bas</b> : {worst['date']} ({worst.get('pv_kwh', 0):.1f} kWh)\n\n"
            msg += f"<i>Bon week-end ! ☀️</i>"

            await self.send_telegram(msg)
            self._mark_sent("weekly_summary")
        except Exception as e:
            logger.error(f"Erreur résumé hebdomadaire: {e}")

    async def _send_daily_summary(self, cache: dict):
        """Envoie le résumé quotidien enrichi."""
        s = self._daily_stats
        now_dt = datetime.now()

        # SoC actuel
        soc_lines = []
        vic = cache.get("victron_system", {}).get("data")
        if vic and vic.get("battery_soc") is not None:
            soc_lines.append(f"  Système : {vic['battery_soc']:.0f}%")

        for grp_key, grp in cache.get("bms_groups", {}).items():
            for uid, unit in grp.get("units", {}).items():
                if unit.get("online") and unit.get("soc") is not None:
                    soc_lines.append(f"  {grp.get('name', grp_key)} #{uid} : {unit['soc']:.0f}%")

        # MPPT yield today
        mppt_lines = []
        total_yield = 0
        for uid, sc in cache.get("solarchargers", {}).get("units", {}).items():
            if sc.get("online"):
                yt = sc.get("yield_today_kwh", 0) or 0
                total_yield += yt
                mppt_lines.append(f"  MPPT #{uid} : {yt:.1f} kWh")

        # Conversion Wh → kWh
        pv_kwh = s["pv_wh_acc"] / 1000
        load_kwh = s["load_wh_acc"] / 1000
        import_kwh = s["import_wh_acc"] / 1000
        export_kwh = s["export_wh_acc"] / 1000

        # Autosuffisance
        self_suff = ((load_kwh - import_kwh) / load_kwh * 100) if load_kwh > 0 else 0
        self_suff = max(0, min(100, self_suff))

        msg = f"📊 <b>Résumé du {now_dt.strftime('%d/%m/%Y')}</b>\n"
        msg += f"{'─' * 28}\n\n"

        msg += f"☀️ <b>Production</b>\n"
        if total_yield > 0:
            msg += f"  Total : {total_yield:.1f} kWh\n"
            for l in mppt_lines:
                msg += f"{l}\n"
        else:
            msg += f"  Estimée : {pv_kwh:.1f} kWh\n"
        msg += f"  Pic : {s['pv_peak_w']:.0f} W\n\n"

        msg += f"🏠 <b>Consommation</b>\n"
        msg += f"  Total : {load_kwh:.1f} kWh\n"
        msg += f"  Pic : {s['load_peak_w']:.0f} W\n\n"

        msg += f"🔌 <b>Réseau</b>\n"
        msg += f"  Import : {import_kwh:.1f} kWh\n"
        msg += f"  Export : {export_kwh:.1f} kWh\n"
        balance = export_kwh - import_kwh
        msg += f"  Balance : {'+' if balance >= 0 else ''}{balance:.1f} kWh "
        msg += f"{'✅' if balance >= 0 else '📉'}\n\n"

        msg += f"🔋 <b>Batteries</b>\n"
        if soc_lines:
            for l in soc_lines:
                msg += f"{l}\n"
        msg += f"  SoC min/max : {s['min_soc']:.0f}% / {s['max_soc']:.0f}%\n\n"

        msg += f"📈 <b>Autosuffisance : {self_suff:.0f}%</b>\n"
        msg += f"🔔 Alertes envoyées : {s['alerts_sent']}"

        await self.send_telegram(msg)

        # Reset daily stats
        self._daily_stats = {
            "alerts_sent": 0, "pv_peak_w": 0, "load_peak_w": 0,
            "min_soc": 100, "max_soc": 0,
            "pv_wh_acc": 0, "load_wh_acc": 0,
            "import_wh_acc": 0, "export_wh_acc": 0, "samples": 0,
        }

    async def send_test(self) -> dict:
        """Envoie un message de test."""
        ok = await self.send_telegram(
            "✅ <b>Smart Energy Hub — Test</b>\n\n"
            "Les alertes Telegram fonctionnent !"
        )
        return {"success": ok}
