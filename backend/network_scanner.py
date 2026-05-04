"""
Smart Energy Hub — Network Scanner (Sprint 10)

Scanne le réseau local pour détecter automatiquement les passerelles et
modules supportés (Voltronic, Victron, Solax, JK-BMS, Pylontech via Elfin,
Waveshare).

Stratégie 3 passes pour rester rapide :
  1. ARP scan du /24 local (lecture /proc/net/arp + ping broadcast)
  2. Probe TCP parallèle sur ports typiques (asyncio, batch de 20)
  3. Identification : pour chaque port ouvert, tentative de lecture
     caractéristique (signature Modbus, banner Voltronic, etc.)

Pas de dépendance externe (uniquement asyncio stdlib + struct).
Tourne sur Pi Zero 2W : ~20-30s pour un /24 typique.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import struct
import subprocess
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Ports caractéristiques à sonder (port → tag de module potentiel)
PROBE_PORTS = {
    502: "modbus_tcp",      # Victron Cerbo, Solax via Waveshare, JK-BMS via gateway
    8899: "voltronic",      # Voltronic/Axpert via passerelle TCP
    9999: "elfin",          # Elfin EE10/EW10 (Pylontech via console RS232)
    23: "telnet",           # Waveshare en mode Telnet (config)
    80: "http",             # interface web Waveshare/autres
}

# Mapping inverse : module_type → port standard
MODULE_DEFAULT_PORTS = {
    "voltronic": 8899,
    "victron": 502,
    "solax": 502,
    "jkbms": 502,
    "pylontech": 9999,
}


@dataclass
class DeviceFound:
    ip: str
    open_ports: list[int] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)        # ex: ["victron_cerbo_likely"]
    suggested_modules: list[str] = field(default_factory=list)  # ex: ["victron", "solax"]
    raw_responses: dict = field(default_factory=dict)     # debug

    def to_dict(self) -> dict:
        return {
            "ip": self.ip,
            "open_ports": sorted(self.open_ports),
            "hints": self.hints,
            "suggested_modules": self.suggested_modules,
        }


# ═══════════════════════════════════════════════════════════════════════════
#  Détection du /24 local
# ═══════════════════════════════════════════════════════════════════════════

def detect_local_cidr() -> Optional[str]:
    """
    Détecte le /24 local en se basant sur la route par défaut.
    Retourne ex: '192.168.1.0/24' ou None.
    """
    try:
        # Connexion UDP fictive vers une IP publique pour découvrir notre IP locale
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        # On suppose un masque /24 (cas le plus courant en résidentiel)
        net = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
        return str(net)
    except Exception as e:
        logger.warning("Impossible de détecter le réseau local: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  ARP scan : lit /proc/net/arp pour récupérer les hôtes "vivants" récents
#  + ping broadcast pour les rendre visibles s'ils ne le sont pas déjà
# ═══════════════════════════════════════════════════════════════════════════

def _read_arp_table() -> set[str]:
    """Lit /proc/net/arp (Linux) et retourne les IPs vues récemment."""
    ips = set()
    try:
        with open("/proc/net/arp", "r") as f:
            next(f)  # skip header
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00":
                    ips.add(parts[0])
    except (OSError, StopIteration):
        pass
    return ips


async def _ping_one(ip: str, timeout: float = 0.5) -> bool:
    """Ping ICMP simple (subprocess pour éviter les permissions raw socket)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(int(timeout) or 1), ip,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0
    except Exception:
        return False


async def arp_sweep(cidr: str, timeout: float = 0.5, max_concurrent: int = 64) -> set[str]:
    """
    Ping tous les hôtes du CIDR pour peupler la table ARP, puis lit la table.
    Retourne le set d'IPs vivantes.
    """
    network = ipaddress.IPv4Network(cidr, strict=False)
    hosts = [str(h) for h in network.hosts()]

    # On évite les /16 ou plus larges (trop long sur Pi Zero)
    if len(hosts) > 1024:
        logger.warning("CIDR %s trop large (%d hôtes) — limitation à 1024", cidr, len(hosts))
        hosts = hosts[:1024]

    sem = asyncio.Semaphore(max_concurrent)

    async def _ping_with_sem(ip):
        async with sem:
            return ip, await _ping_one(ip, timeout=timeout)

    results = await asyncio.gather(*[_ping_with_sem(h) for h in hosts])
    alive = {ip for ip, ok in results if ok}
    # Compléter avec la table ARP (pour les hôtes qui ne répondent pas au ping mais sont vus)
    arp = _read_arp_table()
    alive.update(ip for ip in arp if ip in set(hosts))
    return alive


# ═══════════════════════════════════════════════════════════════════════════
#  Probe TCP : teste l'ouverture des ports caractéristiques
# ═══════════════════════════════════════════════════════════════════════════

async def _try_connect(ip: str, port: int, timeout: float = 1.0) -> bool:
    try:
        fut = asyncio.open_connection(ip, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except (asyncio.TimeoutError, OSError):
        return False


async def probe_ports(ip: str, ports: list[int], timeout: float = 1.0) -> list[int]:
    """Teste tous les ports en parallèle, retourne la liste des ports ouverts."""
    results = await asyncio.gather(*[_try_connect(ip, p, timeout) for p in ports])
    return [p for p, ok in zip(ports, results) if ok]


# ═══════════════════════════════════════════════════════════════════════════
#  Identification : signatures par port/protocole
# ═══════════════════════════════════════════════════════════════════════════

async def _read_modbus_register(ip: str, port: int, unit_id: int, addr: int,
                                count: int = 1, timeout: float = 2.0) -> Optional[bytes]:
    """
    Envoie une requête Modbus TCP Read Holding Registers brute.
    Retourne le payload (bytes) ou None si échec.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
        # MBAP : transaction(2) + protocol(2)=0 + length(2) + unit_id(1) + func(1)=3 + addr(2) + count(2)
        tx_id = 1
        body = struct.pack(">BBHH", unit_id, 0x03, addr, count)
        mbap = struct.pack(">HHH", tx_id, 0, len(body))
        writer.write(mbap + body)
        await writer.drain()
        # Réponse : 7 (header) + 2 (func+bytecount) + 2*count
        header = await asyncio.wait_for(reader.readexactly(8), timeout=timeout)
        if header[7] & 0x80:  # exception
            writer.close()
            return None
        bytecount = await asyncio.wait_for(reader.readexactly(1), timeout=timeout)
        nbytes = bytecount[0]
        payload = await asyncio.wait_for(reader.readexactly(nbytes), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return payload
    except Exception:
        return None


async def _identify_modbus_device(ip: str, port: int = 502) -> dict:
    """
    Tente d'identifier un device Modbus TCP : Victron, Solax, JK-BMS.
    Retourne un dict {hints, suggested}.
    """
    result = {"hints": [], "suggested": []}

    # Victron Cerbo : unit 100 a registre 800 (productname)
    payload = await _read_modbus_register(ip, port, unit_id=100, addr=800, count=8)
    if payload:
        result["hints"].append("victron_cerbo_unit100_responds")
        result["suggested"].append("victron")
        return result

    # Victron MultiPlus : unit 227 a registre 9 (DC voltage)
    payload = await _read_modbus_register(ip, port, unit_id=227, addr=9, count=1)
    if payload:
        result["hints"].append("victron_multiplus_unit227_responds")
        result["suggested"].append("victron")
        return result

    # Solax : unit 1, registre 0 (firmware version) répond
    payload = await _read_modbus_register(ip, port, unit_id=1, addr=0, count=1)
    if payload:
        # On distingue Solax de JK-BMS via un registre supplémentaire
        # JK-BMS répond aussi sur unit 1 mais ses registres standards sont >= 0x1200
        jk = await _read_modbus_register(ip, port, unit_id=1, addr=0x1200, count=2)
        if jk:
            result["hints"].append("jkbms_unit1_register_0x1200")
            result["suggested"].append("jkbms")
        else:
            result["hints"].append("solax_unit1_register_0_responds")
            result["suggested"].append("solax")
    return result


async def _identify_voltronic(ip: str, port: int = 8899) -> dict:
    """
    Voltronic/Axpert : on envoie QID (query device ID) terminé par CR.
    Réponse type : '(98765432101234<CRC><CR>'
    """
    result = {"hints": [], "suggested": []}
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=2.0
        )
        # QID + CRC fixe pour QID = 0xD6 0xEA + CR
        writer.write(b"QID\xd6\xea\r")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(64), timeout=2.0)
        writer.close()
        if data and data.startswith(b"(") and len(data) >= 8:
            result["hints"].append(f"voltronic_qid_responded ({len(data)}b)")
            result["suggested"].append("voltronic")
    except Exception:
        pass
    return result


async def _identify_elfin(ip: str, port: int = 9999) -> dict:
    """
    Elfin EE10/EW10 : sur le port 9999 on a une socket transparente RS232.
    On ne peut pas vraiment 'identifier' sans envoyer une commande Pylontech,
    qui nécessiterait l'adresse de la batterie. On se contente du fait que
    le port 9999 soit ouvert → c'est très probablement un Elfin.
    """
    result = {"hints": [], "suggested": []}
    if await _try_connect(ip, port, timeout=1.0):
        result["hints"].append("port_9999_open_likely_elfin")
        result["suggested"].append("pylontech")
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  NetworkScanner : orchestration
# ═══════════════════════════════════════════════════════════════════════════

class NetworkScanner:
    """Scanner réseau exposé à l'API setup."""

    async def scan(self, cidr: Optional[str] = None,
                   timeout: float = 1.0,
                   max_concurrent: int = 32) -> list[dict]:
        """
        Scan complet : ARP sweep + port probe + identification.
        Retourne la liste des devices trouvés (chacun avec ses suggestions).
        """
        if not cidr:
            cidr = detect_local_cidr()
            if not cidr:
                raise RuntimeError("Impossible de détecter le réseau local — fournir cidr explicitement")

        logger.info("Scan réseau lancé sur %s (timeout=%.1fs)", cidr, timeout)

        # Phase 1 : ARP sweep
        alive = await arp_sweep(cidr, timeout=min(timeout, 1.0), max_concurrent=max_concurrent)
        logger.info("Phase ARP terminée : %d hôte(s) vivants", len(alive))

        if not alive:
            return []

        # Phase 2 : probe ports en parallèle
        sem = asyncio.Semaphore(max_concurrent)

        async def _probe(ip):
            async with sem:
                ports = await probe_ports(ip, list(PROBE_PORTS.keys()), timeout=timeout)
                if not ports:
                    return None
                return DeviceFound(ip=ip, open_ports=ports)

        probe_results = await asyncio.gather(*[_probe(ip) for ip in alive])
        devices = [d for d in probe_results if d is not None]
        logger.info("Phase probe terminée : %d hôte(s) avec port(s) intéressant(s)", len(devices))

        # Phase 3 : identification fine pour chaque device
        async def _identify(dev: DeviceFound):
            if 502 in dev.open_ports:
                r = await _identify_modbus_device(dev.ip, 502)
                dev.hints.extend(r["hints"])
                dev.suggested_modules.extend(r["suggested"])
            if 8899 in dev.open_ports:
                r = await _identify_voltronic(dev.ip, 8899)
                dev.hints.extend(r["hints"])
                dev.suggested_modules.extend(r["suggested"])
            if 9999 in dev.open_ports:
                r = await _identify_elfin(dev.ip, 9999)
                dev.hints.extend(r["hints"])
                dev.suggested_modules.extend(r["suggested"])
            # Dédoublonner
            dev.suggested_modules = list(dict.fromkeys(dev.suggested_modules))

        await asyncio.gather(*[_identify(d) for d in devices])

        results = [d.to_dict() for d in devices]
        logger.info("Scan terminé : %d device(s) identifié(s)", len(results))
        return results

    async def test_module(self, module_type: str, host: str, port: int,
                          extra: Optional[dict] = None) -> dict:
        """
        Teste la connexion à un module spécifique (avec sa configuration).
        Utilisé par le wizard avant de valider une étape.
        """
        extra = extra or {}
        if module_type == "victron":
            payload = await _read_modbus_register(host, port, unit_id=100, addr=800, count=8)
            if payload:
                return {"ok": True, "module": "victron", "detail": "Cerbo GX répond sur unit 100"}
            payload = await _read_modbus_register(host, port, unit_id=227, addr=9, count=1)
            if payload:
                return {"ok": True, "module": "victron", "detail": "MultiPlus détecté sur unit 227"}
            return {"ok": False, "module": "victron", "detail": "Aucun device Victron ne répond"}

        if module_type == "solax":
            unit_id = int(extra.get("unit_id", 1))
            payload = await _read_modbus_register(host, port, unit_id=unit_id, addr=0, count=1)
            if payload:
                return {"ok": True, "module": "solax", "detail": f"Solax répond sur unit {unit_id}"}
            return {"ok": False, "module": "solax", "detail": f"Pas de réponse Modbus sur unit {unit_id}"}

        if module_type == "jkbms":
            unit_id = int(extra.get("unit_id", 1))
            payload = await _read_modbus_register(host, port, unit_id=unit_id, addr=0x1200, count=2)
            if payload:
                return {"ok": True, "module": "jkbms", "detail": f"JK-BMS répond sur unit {unit_id}"}
            return {"ok": False, "module": "jkbms", "detail": f"Pas de réponse JK-BMS sur unit {unit_id}"}

        if module_type == "voltronic":
            r = await _identify_voltronic(host, port)
            if r["suggested"]:
                return {"ok": True, "module": "voltronic", "detail": "Onduleur répond à QID"}
            return {"ok": False, "module": "voltronic", "detail": "Pas de réponse Voltronic"}

        if module_type == "pylontech":
            ok = await _try_connect(host, port, timeout=2.0)
            if ok:
                return {"ok": True, "module": "pylontech", "detail": f"Port {port} ouvert (Elfin probable). Test fonctionnel après save."}
            return {"ok": False, "module": "pylontech", "detail": f"Port {port} fermé"}

        return {"ok": False, "module": module_type, "detail": "Type de module inconnu"}
