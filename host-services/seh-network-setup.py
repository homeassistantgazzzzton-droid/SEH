#!/usr/bin/env python3
"""
Simply Energy Home — Network Setup Service (Sprint 11)

Service systemd qui gère le provisionnement wifi au 1er boot et la résilience
réseau ensuite.

Logique :
  1. Au boot : attendre que le système soit prêt (30s)
  2. Test connectivité Internet (ping 1.1.1.1)
  3. Si OK → exit (l'app Docker prend le relais)
  4. Si KO → mode hotspot :
     - Crée AP "SimplyEnergyHome-XXXX" via NetworkManager
     - Démarre captive portal sur port 80
     - Attend que l'utilisateur configure son wifi
     - Une fois connecté → reboot → retour case départ
  5. Boucle de surveillance : check toutes les 5 min
     Si perte connexion > 5 min → re-bascule en hotspot

Dépend de :
  - NetworkManager (nmcli)
  - dnsmasq (DHCP/DNS du hotspot — géré par NM)
  - iptables (redirection port 80)

Tourne en root (requis pour nmcli + iptables).

Pour tester sans modifier le réseau : lancer avec --dry-run
"""
from __future__ import annotations

import argparse
import http.server
import json
import logging
import os
import re
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

logger = logging.getLogger("seh-network")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ═══════════════════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════════════════

CHECK_INTERVAL_SECONDS = 60         # boucle de surveillance
NO_CONNECTIVITY_GRACE = 300         # 5 min sans connexion avant bascule hotspot
INITIAL_BOOT_DELAY = 30             # attendre que dhcp/wifi finisse au boot
PING_TARGETS = ["1.1.1.1", "8.8.8.8"]
PING_TIMEOUT = 3

HOTSPOT_CON_NAME = "seh-hotspot"
HOTSPOT_IFACE = "wlan0"             # à override par var env si besoin
HOTSPOT_IP_RANGE = "192.168.42.1/24"
HOTSPOT_GW = "192.168.42.1"
PORTAL_PORT = 80

PORTAL_HTML_PATH = "/usr/local/share/seh/captive_portal.html"
STATE_FILE = "/var/lib/seh/network-setup.state"

# Drapeau présent dans /data/seh/wifi-configured une fois la config faite ;
# permet de skipper le hotspot après 1ère utilisation
WIFI_CONFIGURED_FLAG = "/var/lib/seh/wifi-configured"


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers shell
# ═══════════════════════════════════════════════════════════════════════════

class DryRunCommand:
    """Wrapper qui logge les commandes sans les exécuter."""
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run

    def run(self, args: list[str], check: bool = False, timeout: int = 30,
            capture: bool = True) -> subprocess.CompletedProcess:
        if self.dry_run:
            logger.info("[DRY] %s", " ".join(args))
            return subprocess.CompletedProcess(args, 0, "", "")
        logger.debug("exec: %s", " ".join(args))
        return subprocess.run(
            args, check=check, timeout=timeout,
            capture_output=capture, text=True,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Détection identité de l'appareil
# ═══════════════════════════════════════════════════════════════════════════

def get_device_suffix() -> str:
    """
    Génère un suffixe stable de 4 caractères basé sur l'adresse MAC wlan0.
    Permet d'avoir un SSID unique par appareil sans config explicite.
    """
    try:
        mac_path = f"/sys/class/net/{HOTSPOT_IFACE}/address"
        if Path(mac_path).exists():
            mac = Path(mac_path).read_text().strip().replace(":", "")
            return mac[-4:].upper()
    except Exception as e:
        logger.warning("Lecture MAC échouée: %s", e)
    # Fallback : hash du hostname
    h = hash(socket.gethostname()) & 0xFFFF
    return f"{h:04X}"


def hotspot_ssid() -> str:
    return f"SimplyEnergyHome-{get_device_suffix()}"


# ═══════════════════════════════════════════════════════════════════════════
#  Test connectivité Internet
# ═══════════════════════════════════════════════════════════════════════════

def has_internet(timeout: int = PING_TIMEOUT) -> bool:
    """Renvoie True si on peut pinger au moins un target Internet."""
    for target in PING_TARGETS:
        try:
            r = subprocess.run(
                ["ping", "-c", "1", "-W", str(timeout), target],
                capture_output=True, timeout=timeout + 1,
            )
            if r.returncode == 0:
                return True
        except (subprocess.TimeoutExpired, OSError):
            continue
    return False


def has_local_ip(iface: str = None) -> bool:
    """Vérifie qu'on a au moins une IP non-loopback (utile pour LAN-only)."""
    try:
        r = subprocess.run(["ip", "-4", "addr"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", line)
            if m and not m.group(1).startswith("127.") and not m.group(1).startswith("192.168.42."):
                return True
    except Exception as e:
        logger.warning("Lecture IP échouée: %s", e)
    return False


# ═══════════════════════════════════════════════════════════════════════════
#  NetworkManager wrapper
# ═══════════════════════════════════════════════════════════════════════════

class NetworkManager:
    def __init__(self, cmd: DryRunCommand):
        self.cmd = cmd

    def is_available(self) -> bool:
        try:
            r = self.cmd.run(["nmcli", "--version"], timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    def list_wifi(self) -> list[dict]:
        """Renvoie la liste des SSIDs visibles (scan rapide)."""
        try:
            # Forcer un rescan pour avoir des résultats frais
            self.cmd.run(["nmcli", "device", "wifi", "rescan"], timeout=15)
            time.sleep(2)
            r = self.cmd.run([
                "nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE",
                "device", "wifi", "list",
            ], timeout=10)
            networks = []
            seen = set()
            for line in r.stdout.splitlines():
                # Format : SSID:SIGNAL:SECURITY:IN-USE
                # SSID peut contenir ':' échappé en \:
                parts = re.split(r'(?<!\\):', line)
                if len(parts) < 4:
                    continue
                ssid = parts[0].replace(r'\:', ':').strip()
                if not ssid or ssid in seen:
                    continue
                seen.add(ssid)
                try:
                    signal_strength = int(parts[1])
                except ValueError:
                    signal_strength = 0
                security = parts[2].strip()
                in_use = parts[3].strip() == "*"
                networks.append({
                    "ssid": ssid,
                    "signal": signal_strength,
                    "security": security or "Open",
                    "in_use": in_use,
                })
            networks.sort(key=lambda n: -n["signal"])
            return networks
        except Exception as e:
            logger.error("Scan wifi échoué: %s", e)
            return []

    def hotspot_active(self) -> bool:
        try:
            r = self.cmd.run([
                "nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active",
            ], timeout=5)
            for line in r.stdout.splitlines():
                if line.startswith(HOTSPOT_CON_NAME + ":"):
                    return True
        except Exception:
            pass
        return False

    def start_hotspot(self) -> bool:
        ssid = hotspot_ssid()
        logger.info("Démarrage hotspot SSID=%s sur %s", ssid, HOTSPOT_IFACE)

        # Supprimer une éventuelle config résiduelle
        self.cmd.run(["nmcli", "connection", "delete", HOTSPOT_CON_NAME], timeout=10)

        # Créer le profil AP (open, sans mot de passe — c'est un portail captif)
        r = self.cmd.run([
            "nmcli", "connection", "add",
            "type", "wifi", "ifname", HOTSPOT_IFACE,
            "con-name", HOTSPOT_CON_NAME,
            "autoconnect", "no",
            "ssid", ssid,
            "mode", "ap",
            "ipv4.method", "shared",
            "ipv4.addresses", HOTSPOT_IP_RANGE,
            "ipv6.method", "ignore",
            "wifi.band", "bg",
            "wifi.channel", "6",
            "wifi-sec.key-mgmt", "none",
        ], timeout=15)
        if r.returncode != 0:
            logger.error("nmcli add hotspot a échoué: %s", r.stderr)
            return False

        # Activer
        r = self.cmd.run(["nmcli", "connection", "up", HOTSPOT_CON_NAME], timeout=20)
        if r.returncode != 0:
            logger.error("Activation hotspot échouée: %s", r.stderr)
            return False

        logger.info("Hotspot actif sur %s @ %s", HOTSPOT_IFACE, HOTSPOT_GW)
        return True

    def stop_hotspot(self):
        logger.info("Arrêt hotspot")
        self.cmd.run(["nmcli", "connection", "down", HOTSPOT_CON_NAME], timeout=10)
        self.cmd.run(["nmcli", "connection", "delete", HOTSPOT_CON_NAME], timeout=10)

    def connect_wifi(self, ssid: str, password: str | None) -> tuple[bool, str]:
        """Tente de se connecter à un wifi. Retourne (ok, message)."""
        logger.info("Tentative connexion à SSID=%s", ssid)
        args = ["nmcli", "device", "wifi", "connect", ssid, "ifname", HOTSPOT_IFACE]
        if password:
            args += ["password", password]
        try:
            r = self.cmd.run(args, timeout=45)
            if r.returncode == 0:
                logger.info("Connexion réussie à %s", ssid)
                return True, "Connecté"
            err = (r.stderr or r.stdout).strip()
            logger.warning("Connexion échouée: %s", err)
            return False, err or "Échec connexion"
        except subprocess.TimeoutExpired:
            return False, "Timeout (45s) — vérifiez le mot de passe et la portée"


# ═══════════════════════════════════════════════════════════════════════════
#  iptables : redirige tout le HTTP vers le portail captif
# ═══════════════════════════════════════════════════════════════════════════

class IptablesCaptive:
    """Active/désactive la redirection iptables qui force le portail captif."""

    def __init__(self, cmd: DryRunCommand, gw: str = HOTSPOT_GW, dst_port: int = PORTAL_PORT):
        self.cmd = cmd
        self.gw = gw
        self.dst_port = dst_port

    def enable(self):
        # Redirige tout TCP/80 entrant sur l'interface AP vers notre portail
        # (déjà sur 80 si on est sur 80, mais on garde la règle pour cohérence
        # si un jour on change PORTAL_PORT)
        self.cmd.run([
            "iptables", "-t", "nat", "-A", "PREROUTING",
            "-i", HOTSPOT_IFACE, "-p", "tcp", "--dport", "80",
            "-j", "REDIRECT", "--to-port", str(self.dst_port),
        ])
        # Idem pour HTTPS (on log les hits ; le client verra une erreur cert
        # mais sera redirigé sur Android/iOS car ils testent HTTP en premier)
        # → on ne touche pas HTTPS pour éviter les problèmes de cert
        logger.info("Redirection iptables activée (HTTP → :%d)", self.dst_port)

    def disable(self):
        self.cmd.run([
            "iptables", "-t", "nat", "-D", "PREROUTING",
            "-i", HOTSPOT_IFACE, "-p", "tcp", "--dport", "80",
            "-j", "REDIRECT", "--to-port", str(self.dst_port),
        ])


# ═══════════════════════════════════════════════════════════════════════════
#  Serveur HTTP du portail captif
# ═══════════════════════════════════════════════════════════════════════════

class CaptivePortalState:
    """État partagé entre les threads HTTP."""
    def __init__(self):
        self.last_connect_attempt: dict | None = None  # {ssid, ok, message, ts}
        self.connect_in_progress: bool = False
        self.lock = threading.Lock()


class CaptivePortalHandler(http.server.BaseHTTPRequestHandler):
    """
    Sert le portail captif et gère les endpoints /scan et /connect.

    Tous les Apple/Android probes de connectivité (generate_204, hotspot-detect,
    connectivitycheck, etc.) reçoivent une page HTML qui déclenche la
    notification "Se connecter à ce réseau" sur le téléphone.
    """

    nm: NetworkManager = None      # injecté
    state: CaptivePortalState = None  # injecté
    portal_html: str = ""          # HTML chargé depuis disk

    def log_message(self, fmt, *args):
        logger.info("portal %s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self._send(code, body, ctype="application/json")

    def _send_portal(self):
        self._send(200, self.portal_html.encode("utf-8"))

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        # API
        if path == "/scan":
            networks = self.nm.list_wifi()
            self._send_json(200, {"networks": networks})
            return

        if path == "/connect_status":
            with self.state.lock:
                self._send_json(200, {
                    "in_progress": self.state.connect_in_progress,
                    "last_attempt": self.state.last_connect_attempt,
                })
            return

        # Tous les autres GET (y compris /generate_204, /hotspot-detect.html,
        # /connecttest.txt, /redirect, /, etc.) → on sert le portail.
        # Cela déclenche le captive portal sur Android/iOS/Windows.
        self._send_portal()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""

        if path == "/connect":
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": "JSON invalide"})
                return

            ssid = (body.get("ssid") or "").strip()
            password = body.get("password") or None
            if not ssid:
                self._send_json(400, {"error": "SSID manquant"})
                return

            with self.state.lock:
                if self.state.connect_in_progress:
                    self._send_json(409, {"error": "Connexion déjà en cours"})
                    return
                self.state.connect_in_progress = True

            # On lance la connexion dans un thread pour répondre tout de suite
            def _do_connect():
                ok, msg = self.nm.connect_wifi(ssid, password)
                with self.state.lock:
                    self.state.last_connect_attempt = {
                        "ssid": ssid, "ok": ok, "message": msg, "ts": time.time(),
                    }
                    self.state.connect_in_progress = False
                if ok:
                    # Marquer comme configuré
                    try:
                        Path(WIFI_CONFIGURED_FLAG).parent.mkdir(parents=True, exist_ok=True)
                        Path(WIFI_CONFIGURED_FLAG).write_text(ssid)
                    except OSError:
                        pass
                    # Le watcher principal se chargera de fermer le hotspot
                    # et de signaler "ok" au prochain check

            threading.Thread(target=_do_connect, daemon=True).start()
            self._send_json(202, {"status": "connecting", "ssid": ssid})
            return

        self._send(404, b"Not found", ctype="text/plain")


class CaptivePortalServer:
    def __init__(self, nm: NetworkManager, port: int = PORTAL_PORT):
        self.nm = nm
        self.port = port
        self.httpd: socketserver.TCPServer | None = None
        self.thread: threading.Thread | None = None
        self.state = CaptivePortalState()

    def start(self):
        # Charger le HTML
        try:
            html = Path(PORTAL_HTML_PATH).read_text(encoding="utf-8")
        except OSError as e:
            logger.error("Impossible de lire %s: %s", PORTAL_HTML_PATH, e)
            html = "<h1>Simply Energy Home — Configuration wifi</h1><p>Page indisponible.</p>"

        # Injecter état dans le handler (class attrs partagés OK car single server)
        CaptivePortalHandler.nm = self.nm
        CaptivePortalHandler.state = self.state
        CaptivePortalHandler.portal_html = html

        # ThreadingTCPServer pour servir plusieurs clients en // pendant le scan
        class ReusableTCPServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.httpd = ReusableTCPServer(("0.0.0.0", self.port), CaptivePortalHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        logger.info("Portail captif en écoute sur :%d", self.port)

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            logger.info("Portail captif arrêté")

    def wifi_configured(self) -> bool:
        """Indique qu'une tentative de connexion a réussi (pour sortir du hotspot)."""
        with self.state.lock:
            la = self.state.last_connect_attempt
        return bool(la and la.get("ok"))


# ═══════════════════════════════════════════════════════════════════════════
#  Boucle principale
# ═══════════════════════════════════════════════════════════════════════════

class NetworkSetupService:
    def __init__(self, dry_run: bool = False):
        self.cmd = DryRunCommand(dry_run)
        self.nm = NetworkManager(self.cmd)
        self.iptables = IptablesCaptive(self.cmd)
        self.portal = CaptivePortalServer(self.nm)
        self.no_internet_since: float | None = None
        self.in_hotspot_mode: bool = False
        self._stop = threading.Event()

    def _enter_hotspot_mode(self):
        if self.in_hotspot_mode:
            return
        logger.info("=== Bascule en mode HOTSPOT ===")
        if not self.nm.start_hotspot():
            logger.error("Démarrage hotspot impossible")
            return
        time.sleep(3)  # laisser NetworkManager activer l'AP
        self.iptables.enable()
        self.portal.start()
        self.in_hotspot_mode = True
        logger.info("Hotspot opérationnel — SSID '%s' — accédez à http://%s/",
                    hotspot_ssid(), HOTSPOT_GW)

    def _exit_hotspot_mode(self):
        if not self.in_hotspot_mode:
            return
        logger.info("=== Sortie du mode HOTSPOT ===")
        self.portal.stop()
        self.iptables.disable()
        self.nm.stop_hotspot()
        self.in_hotspot_mode = False
        # Attendre que NM remette le wifi en mode client si possible
        time.sleep(3)

    def stop(self, *_):
        logger.info("Signal d'arrêt reçu")
        self._stop.set()

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        if not self.nm.is_available():
            logger.error("NetworkManager (nmcli) introuvable. Installation requise.")
            sys.exit(1)

        # Si Ethernet est branché et fonctionnel → on saute tout
        logger.info("Délai initial %ds (laisser DHCP/wifi essayer)…", INITIAL_BOOT_DELAY)
        for _ in range(INITIAL_BOOT_DELAY):
            if self._stop.is_set():
                return
            time.sleep(1)

        while not self._stop.is_set():
            internet = has_internet()
            now = time.time()

            if internet:
                # Tout va bien
                self.no_internet_since = None
                if self.in_hotspot_mode:
                    # Cas peu probable : l'utilisateur s'est connecté ET on a aussi eth
                    self._exit_hotspot_mode()
                logger.debug("Connectivité OK")
            else:
                # Pas de connexion
                if self.in_hotspot_mode:
                    # Vérifier si l'utilisateur a configuré son wifi via le portail
                    if self.portal.wifi_configured():
                        logger.info("Wifi configuré via portail — sortie hotspot et reboot dans 5s")
                        self._exit_hotspot_mode()
                        time.sleep(5)
                        # Reboot pour repartir propre (DHCP, mDNS, etc.)
                        if not self.cmd.dry_run:
                            subprocess.Popen(["systemctl", "reboot"])
                        return
                else:
                    # On note depuis quand on est sans connexion
                    if self.no_internet_since is None:
                        self.no_internet_since = now
                        logger.warning("Pas de connectivité Internet")
                    elif (now - self.no_internet_since) >= NO_CONNECTIVITY_GRACE:
                        logger.warning(
                            "Pas de connectivité depuis %ds → bascule hotspot",
                            int(now - self.no_internet_since),
                        )
                        self._enter_hotspot_mode()

            # Attente jusqu'au prochain check (interrompable)
            for _ in range(CHECK_INTERVAL_SECONDS):
                if self._stop.is_set():
                    break
                time.sleep(1)

        # Cleanup
        if self.in_hotspot_mode:
            self._exit_hotspot_mode()
        logger.info("Service arrêté proprement")


# ═══════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="SEH Network Setup Service")
    parser.add_argument("--dry-run", action="store_true",
                        help="N'exécute pas les commandes système, log seulement")
    parser.add_argument("--once", action="store_true",
                        help="Vérifie une fois et quitte (test connectivité)")
    parser.add_argument("--hotspot", action="store_true",
                        help="Force le mode hotspot et reste dedans (debug)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    if args.once:
        cmd = DryRunCommand(args.dry_run)
        nm = NetworkManager(cmd)
        print("nmcli disponible:", nm.is_available())
        print("Internet:", has_internet())
        print("IP locale:", has_local_ip())
        print("SSID hotspot ce serait:", hotspot_ssid())
        if nm.is_available():
            print("Wifi visibles:")
            for n in nm.list_wifi()[:10]:
                print(f"  {n['signal']:>3}% [{n['security']:<10}] {n['ssid']}")
        return

    service = NetworkSetupService(dry_run=args.dry_run)

    if args.hotspot:
        # Mode debug : bascule direct en hotspot et reste
        service._enter_hotspot_mode()
        try:
            while True:
                time.sleep(5)
                if service.portal.wifi_configured():
                    print("Wifi configuré — Ctrl-C pour quitter")
        except KeyboardInterrupt:
            service._exit_hotspot_mode()
        return

    service.run()


if __name__ == "__main__":
    main()
