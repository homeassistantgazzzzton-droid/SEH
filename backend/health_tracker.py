"""
Smart Energy Hub — Health Tracker (Sprint 15)

Suit les events de santé de l'application sur 24h pour détecter les patterns
de problèmes (pannes, redémarrages, polling timeouts).

Architecture :
  - Stocke les events en mémoire (deque limitée à 24h)
  - Persistance simple via /data/health_events.json (snapshot à chaque event)
  - Snapshot temps réel + historique 24h en barres horaires

Types d'events trackés :
  - inverter_ok / inverter_fail
  - bms_ok / bms_fail
  - poll_timeout (latence anormale)
  - app_start (démarrage de l'app)
  - module_error (erreur générique d'un module)

Usage côté code :
    from health_tracker import get_tracker
    get_tracker().record("inverter_fail", "Voltronic timeout sur QID")

Le dashboard regroupe par heure et calcule un score de santé 0-100.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

EVENTS_FILE = "/data/health_events.json"
RETENTION_SECONDS = 24 * 3600
MAX_EVENTS = 5000          # cap dur pour éviter qu'un module qui spamme remplisse la RAM

# Types d'events qui indiquent un problème
FAIL_TYPES = {"inverter_fail", "bms_fail", "poll_timeout", "module_error",
              "auth_fail", "websocket_drop"}
OK_TYPES = {"inverter_ok", "bms_ok", "app_start"}


@dataclass
class HealthEvent:
    ts: float
    type: str       # ex: "inverter_fail"
    detail: str     # message court (max 200 chars)

    def to_dict(self) -> dict:
        return asdict(self)


# ═══════════════════════════════════════════════════════════════════════════
#  HealthTracker
# ═══════════════════════════════════════════════════════════════════════════

class HealthTracker:
    def __init__(self, persist_path: str = EVENTS_FILE):
        self._events: deque[HealthEvent] = deque(maxlen=MAX_EVENTS)
        self._lock = threading.Lock()
        self._persist_path = persist_path
        self._dirty = False
        self._persist_lock = threading.Lock()
        self._load_from_disk()

    def _load_from_disk(self):
        """Charge les events sauvés du dernier run (rétention 24h)."""
        try:
            with open(self._persist_path, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        cutoff = time.time() - RETENTION_SECONDS
        for evt in data.get("events", []):
            if evt.get("ts", 0) >= cutoff:
                self._events.append(HealthEvent(
                    ts=evt["ts"], type=evt["type"], detail=evt.get("detail", "")
                ))
        logger.info("HealthTracker: %d event(s) chargés depuis %s",
                    len(self._events), self._persist_path)

    def _persist(self):
        """Sauvegarde sur disque (best-effort)."""
        try:
            Path(self._persist_path).parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"events": [e.to_dict() for e in self._events]}, f)
            os.replace(tmp, self._persist_path)
        except OSError as e:
            logger.warning("HealthTracker persist failed: %s", e)

    def _purge_old(self):
        cutoff = time.time() - RETENTION_SECONDS
        # On évite de muter pendant qu'on itère : on vide tant que tête trop ancienne
        while self._events and self._events[0].ts < cutoff:
            self._events.popleft()

    def record(self, event_type: str, detail: str = ""):
        """Enregistre un event. Thread-safe. Persiste de façon throttlée."""
        detail = (detail or "")[:200]  # cap de sécurité
        evt = HealthEvent(ts=time.time(), type=event_type, detail=detail)
        with self._lock:
            self._events.append(evt)
            self._purge_old()
            self._dirty = True

        # Persistance throttlée : pas plus d'1 fois par 30s
        # On ne bloque pas l'appelant — thread daemon dédié plus loin

    def snapshot(self) -> dict:
        """État courant : compte par catégorie sur la dernière heure."""
        with self._lock:
            self._purge_old()
            now = time.time()
            cutoff = now - 3600
            recent = [e for e in self._events if e.ts >= cutoff]

        counts = defaultdict(int)
        for e in recent:
            counts[e.type] += 1

        fails = sum(c for t, c in counts.items() if t in FAIL_TYPES)
        oks = sum(c for t, c in counts.items() if t in OK_TYPES)
        total = fails + oks
        score = 100 if total == 0 else max(0, int(100 * oks / total))

        return {
            "score_1h": score,
            "counts_1h": dict(counts),
            "fails_1h": fails,
            "oks_1h": oks,
            "total_events_24h": len(self._events),
        }

    def hourly_buckets(self) -> list[dict]:
        """Renvoie 24 buckets horaires avec count fail/ok par bucket."""
        with self._lock:
            self._purge_old()
            evts = list(self._events)

        now = time.time()
        # 24 buckets, chacun couvre 1h, indexé de l'heure passée la plus ancienne
        buckets = []
        for i in range(24, 0, -1):
            start = now - i * 3600
            end = now - (i - 1) * 3600
            in_bucket = [e for e in evts if start <= e.ts < end]
            fails = sum(1 for e in in_bucket if e.type in FAIL_TYPES)
            oks = sum(1 for e in in_bucket if e.type in OK_TYPES)
            total = fails + oks
            score = 100 if total == 0 else max(0, int(100 * oks / total))
            buckets.append({
                "start": start,
                "end": end,
                "fails": fails,
                "oks": oks,
                "score": score,
                "events": len(in_bucket),
            })
        return buckets

    def recent_failures(self, max_n: int = 30) -> list[dict]:
        """Retourne les N derniers events de type fail, du plus récent au plus ancien."""
        with self._lock:
            evts = [e for e in self._events if e.type in FAIL_TYPES]
        evts.sort(key=lambda e: e.ts, reverse=True)
        return [e.to_dict() for e in evts[:max_n]]

    def all_events(self) -> list[dict]:
        with self._lock:
            self._purge_old()
            return [e.to_dict() for e in self._events]


# ═══════════════════════════════════════════════════════════════════════════
#  Persistence thread (background, throttle 30s)
# ═══════════════════════════════════════════════════════════════════════════

def _persist_loop(tracker: "HealthTracker", stop_event: threading.Event):
    while not stop_event.is_set():
        stop_event.wait(30)
        if stop_event.is_set():
            break
        with tracker._lock:
            dirty = tracker._dirty
            tracker._dirty = False
        if dirty:
            with tracker._persist_lock:
                tracker._persist()


# ═══════════════════════════════════════════════════════════════════════════
#  Singleton
# ═══════════════════════════════════════════════════════════════════════════

_tracker: Optional[HealthTracker] = None
_stop_event: Optional[threading.Event] = None


def get_tracker() -> HealthTracker:
    global _tracker, _stop_event
    if _tracker is None:
        _tracker = HealthTracker()
        _stop_event = threading.Event()
        t = threading.Thread(target=_persist_loop, args=(_tracker, _stop_event),
                             name="health-persist", daemon=True)
        t.start()
        _tracker.record("app_start", "HealthTracker initialisé")
    return _tracker


def shutdown():
    """À appeler au shutdown propre pour persister une dernière fois."""
    global _tracker, _stop_event
    if _stop_event:
        _stop_event.set()
    if _tracker:
        with _tracker._persist_lock:
            _tracker._persist()
