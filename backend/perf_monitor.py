"""
Smart Energy Hub — Performance Monitor (Sprint 13)

Mesure en continu :
  - CPU usage (% du process et global)
  - RAM (RSS du process)
  - Latence des boucles de polling (moyenne mobile)
  - Décision adaptive : si charge trop haute, signale qu'il faut ralentir le polling

Conçu pour fonctionner SANS psutil (dépendance trop lourde sur Pi Zero 2W).
Lit directement /proc/[pid]/stat et /proc/stat.

Exposé via :
  - /api/perf            : snapshot temps réel
  - /api/perf/history    : 60 dernières minutes
  - perf_monitor.should_throttle()  : décision pour la boucle de polling
"""
from __future__ import annotations

import logging
import os
import platform
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
#  Lecture /proc (Linux) — sans psutil
# ═══════════════════════════════════════════════════════════════════════════

# os.sysconf / os.sysconf_names n'existent que sur Unix. Sur Windows on prend
# les valeurs par défaut Linux usuelles (impact négligeable car le perf monitor
# ne sert vraiment que sur Pi/Linux ; sur Windows il dégrade silencieusement).
if hasattr(os, "sysconf_names") and hasattr(os, "sysconf"):
    try:
        CLK_TCK = os.sysconf("SC_CLK_TCK") if "SC_CLK_TCK" in os.sysconf_names else 100
    except (ValueError, OSError):
        CLK_TCK = 100
    try:
        PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if "SC_PAGE_SIZE" in os.sysconf_names else 4096
    except (ValueError, OSError):
        PAGE_SIZE = 4096
else:
    CLK_TCK = 100
    PAGE_SIZE = 4096


def _read_proc_stat() -> Optional[tuple[int, int]]:
    """
    Retourne (total_jiffies, idle_jiffies) du système.
    Renvoie None si /proc/stat indisponible (non-Linux).
    """
    try:
        with open("/proc/stat", "r") as f:
            line = f.readline()
        # cpu  user nice system idle iowait irq softirq steal guest guest_nice
        parts = line.split()
        if parts[0] != "cpu":
            return None
        nums = [int(x) for x in parts[1:8]]
        idle = nums[3] + nums[4]  # idle + iowait
        total = sum(nums)
        return total, idle
    except (FileNotFoundError, IndexError, ValueError):
        return None


def _read_proc_pid_stat(pid: int = None) -> Optional[tuple[int, int]]:
    """
    Retourne (total_jiffies_du_process, rss_bytes) pour le pid donné.
    On utilise /proc/[pid]/stat pour les jiffies et /proc/[pid]/statm pour RSS
    car certains environnements (sandbox containers) renvoient des RSS aberrants
    via le champ 23 de /proc/[pid]/stat.
    """
    pid = pid or os.getpid()
    try:
        # CPU jiffies depuis /proc/[pid]/stat
        with open(f"/proc/{pid}/stat", "r") as f:
            data = f.read()
        rparen = data.rindex(")")
        rest = data[rparen + 2:].split()
        utime = int(rest[11])
        stime = int(rest[12])
        jiffies = utime + stime

        # RSS depuis /proc/[pid]/statm (plus fiable)
        # Format : size resident shared text lib data dt   (en pages)
        with open(f"/proc/{pid}/statm", "r") as f:
            parts = f.read().split()
        rss_pages = int(parts[1])
        return jiffies, rss_pages * PAGE_SIZE
    except (FileNotFoundError, IndexError, ValueError):
        return None


def _read_meminfo() -> Optional[dict]:
    """Lit /proc/meminfo et retourne MemTotal/MemAvailable en bytes."""
    try:
        with open("/proc/meminfo", "r") as f:
            data = f.read()
        result = {}
        for line in data.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            val = val.strip().split()
            if not val:
                continue
            kb = int(val[0])
            result[key.strip()] = kb * 1024
        return result
    except (FileNotFoundError, ValueError):
        return None


def _read_temperature() -> Optional[float]:
    """Lit la température CPU (Pi/Linux). Retourne en °C ou None."""
    paths = [
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/devices/virtual/thermal/thermal_zone0/temp",
    ]
    for p in paths:
        try:
            with open(p, "r") as f:
                v = int(f.read().strip())
            return v / 1000.0
        except (FileNotFoundError, ValueError, PermissionError):
            continue
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  Sample
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class PerfSample:
    ts: float                         # timestamp unix
    cpu_global_pct: float = 0.0       # CPU global %
    cpu_proc_pct: float = 0.0         # CPU process %
    rss_mb: float = 0.0               # RAM process (RSS) en Mo
    mem_used_pct: float = 0.0         # RAM système %
    mem_available_mb: float = 0.0     # RAM dispo en Mo
    cpu_temp_c: Optional[float] = None
    poll_latency_ms: float = 0.0      # latence moyenne du polling
    poll_count: int = 0               # nb de cycles de poll dans la dernière fenêtre
    throttled: bool = False           # le polling a-t-il été ralenti ?

    def to_dict(self) -> dict:
        return asdict(self)


# ═══════════════════════════════════════════════════════════════════════════
#  PerfMonitor
# ═══════════════════════════════════════════════════════════════════════════

class PerfMonitor:
    """
    Tourne dans un thread daemon, échantillonne toutes les 5 secondes.
    Maintient un historique de 60 min (720 samples).
    """

    SAMPLE_INTERVAL = 5
    HISTORY_LEN = 720          # 60 min @ 5s
    THROTTLE_CPU = 80.0        # %
    THROTTLE_DURATION = 30     # s avant d'enclencher throttle

    def __init__(self):
        self._history: deque[PerfSample] = deque(maxlen=self.HISTORY_LEN)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # État pour calcul des % CPU (deltas entre samples)
        self._prev_total: Optional[int] = None
        self._prev_idle: Optional[int] = None
        self._prev_proc: Optional[int] = None

        # Latence polling (moyenne mobile sur 60s)
        self._poll_latencies: deque[float] = deque(maxlen=12)  # 12 × 5s = 60s
        self._poll_count_window = 0

        # Throttle
        self._high_cpu_since: Optional[float] = None
        self._throttled = False

        # Caps statiques
        self.platform = platform.machine()
        self.is_pi = self._detect_pi()
        self.is_low_resource = self._detect_low_resource()

    def _detect_pi(self) -> bool:
        try:
            with open("/sys/firmware/devicetree/base/model", "r") as f:
                model = f.read().lower()
            return "raspberry" in model
        except (FileNotFoundError, OSError):
            return False

    def _detect_low_resource(self) -> bool:
        """True si <= 1 Go RAM (Pi Zero 2W = 512 Mo)."""
        m = _read_meminfo()
        if not m:
            return False
        return m.get("MemTotal", 0) <= 1024 * 1024 * 1024

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="perf-monitor", daemon=True)
        self._thread.start()
        logger.info("PerfMonitor démarré (Pi=%s, low_res=%s)", self.is_pi, self.is_low_resource)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        # Premier échantillon : initialise les compteurs sans calcul
        self._sample(initial=True)
        while not self._stop.is_set():
            self._stop.wait(self.SAMPLE_INTERVAL)
            if self._stop.is_set():
                break
            try:
                self._sample()
            except Exception as e:
                logger.warning("PerfMonitor sample error: %s", e)

    def _sample(self, initial: bool = False):
        now = time.time()
        s = PerfSample(ts=now)

        # CPU global
        sys_stat = _read_proc_stat()
        if sys_stat:
            total, idle = sys_stat
            if not initial and self._prev_total is not None:
                d_total = max(1, total - self._prev_total)
                d_idle = idle - self._prev_idle
                s.cpu_global_pct = round(100.0 * (d_total - d_idle) / d_total, 1)
            self._prev_total = total
            self._prev_idle = idle

        # CPU process
        proc_stat = _read_proc_pid_stat()
        if proc_stat:
            proc_jiffies, rss_bytes = proc_stat
            s.rss_mb = round(rss_bytes / 1024 / 1024, 1)
            if not initial and self._prev_proc is not None and sys_stat:
                d_proc = proc_jiffies - self._prev_proc
                d_total = max(1, sys_stat[0] - self._prev_total + (sys_stat[0] - self._prev_total))
                # Approximation : CPU% process = d_proc / (CLK_TCK * interval)
                s.cpu_proc_pct = round(100.0 * d_proc / (CLK_TCK * self.SAMPLE_INTERVAL), 1)
            self._prev_proc = proc_jiffies

        # RAM système
        mem = _read_meminfo()
        if mem:
            total = mem.get("MemTotal", 0)
            available = mem.get("MemAvailable", mem.get("MemFree", 0))
            if total > 0:
                s.mem_used_pct = round(100.0 * (1 - available / total), 1)
                s.mem_available_mb = round(available / 1024 / 1024, 1)

        # Température
        s.cpu_temp_c = _read_temperature()

        # Latence polling (moyenne sur la fenêtre)
        if self._poll_latencies:
            s.poll_latency_ms = round(sum(self._poll_latencies) / len(self._poll_latencies), 1)
        s.poll_count = self._poll_count_window
        self._poll_count_window = 0

        # Décision throttle
        if s.cpu_global_pct >= self.THROTTLE_CPU:
            if self._high_cpu_since is None:
                self._high_cpu_since = now
            elif (now - self._high_cpu_since) >= self.THROTTLE_DURATION:
                if not self._throttled:
                    logger.warning(
                        "CPU > %.0f%% depuis %ds → throttle activé",
                        self.THROTTLE_CPU, int(now - self._high_cpu_since),
                    )
                self._throttled = True
        else:
            if self._throttled and s.cpu_global_pct < (self.THROTTLE_CPU - 20):
                logger.info("CPU revenu à %.1f%% → throttle désactivé", s.cpu_global_pct)
                self._throttled = False
            self._high_cpu_since = None
        s.throttled = self._throttled

        with self._lock:
            self._history.append(s)

    # ── API publique ────────────────────────────────────────────────────

    def report_poll_cycle(self, latency_ms: float):
        """À appeler par les boucles de polling pour reporter leur latence."""
        self._poll_latencies.append(latency_ms)
        self._poll_count_window += 1

    def should_throttle(self) -> bool:
        """Renvoie True si le polling devrait ralentir."""
        return self._throttled

    def get_throttle_factor(self) -> float:
        """
        Multiplicateur d'intervalle de polling :
          1.0 = normal
          2.0 = 2x plus lent (si throttled)
          3.0 = 3x si CPU > 90% sur Pi Zero
        """
        if not self._throttled:
            return 1.0
        latest = self.snapshot()
        if latest and latest.cpu_global_pct >= 90:
            return 3.0
        return 2.0

    def snapshot(self) -> Optional[PerfSample]:
        """Dernier sample."""
        with self._lock:
            if not self._history:
                return None
            return self._history[-1]

    def history(self, last_n: int = 60) -> list[dict]:
        """Renvoie les `last_n` derniers samples."""
        with self._lock:
            samples = list(self._history)[-last_n:]
        return [s.to_dict() for s in samples]

    def info(self) -> dict:
        """Infos système statiques."""
        mem = _read_meminfo() or {}
        return {
            "platform": self.platform,
            "is_pi": self.is_pi,
            "is_low_resource": self.is_low_resource,
            "mem_total_mb": round(mem.get("MemTotal", 0) / 1024 / 1024, 0),
            "cpu_count": os.cpu_count() or 1,
            "throttle_cpu_threshold": self.THROTTLE_CPU,
        }


# ═══════════════════════════════════════════════════════════════════════════
#  Singleton + helper context manager
# ═══════════════════════════════════════════════════════════════════════════

_monitor: Optional[PerfMonitor] = None


def get_monitor() -> PerfMonitor:
    global _monitor
    if _monitor is None:
        _monitor = PerfMonitor()
    return _monitor


def init_monitor() -> PerfMonitor:
    m = get_monitor()
    m.start()
    return m


class measure_poll:
    """
    Context manager pour mesurer la durée d'un cycle de polling et la reporter.

    Usage :
        with measure_poll():
            await poll_voltronic()
    """
    def __enter__(self):
        self._t0 = time.monotonic()
        return self

    def __exit__(self, *args):
        dt_ms = (time.monotonic() - self._t0) * 1000
        get_monitor().report_poll_cycle(dt_ms)
