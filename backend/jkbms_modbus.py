"""
JK-BMS Modbus RTU Direct Client
Protocole : JK BMS RS485 Modbus V1.0 / V1.1
Modèles   : PB2A16S20P, PB2A16S15P, PB1A16S15P, PB1A16S10P (FW≥19)

Connexion possible via :
  - Passerelle TCP/IP (Elfin EW10, EW11, Waveshare…)  → mode TCP
  - Adaptateur USB→RS485 (CH340, FTDI, CP2102)         → mode RTU/Serial

Registres utilisés (lecture seule, fonction 0x03) :
  Base 0x1200 : données temps réel (tensions cellules, pack, courant, temp, SoC…)
  Base 0x1400 : informations statiques (modèle, version, serial)

Toutes les valeurs numériques sont en milli-unités sauf indication contraire.
"""
import asyncio
import logging
import os
import struct
from dataclasses import dataclass, field
from typing import Optional

from pymodbus.client import AsyncModbusTcpClient, AsyncModbusSerialClient
from pymodbus.exceptions import ModbusException
from pymodbus.framer import FramerType

logger = logging.getLogger(__name__)

# ── Registres temps réel (base 0x1200) ──────────────────────────────────────
# Chaque registre Modbus = 2 octets. Les adresses ci-dessous sont les offsets
# depuis 0x1200, en nombre de registres (×2 octets).

BASE_RT   = 0x1200   # Real-time data
BASE_CFG  = 0x1000   # Configuration (settings)
BASE_INFO = 0x1400   # Device info / static

# Registres individuels cells (UINT16, mV) : offsets 0..31 depuis BASE_RT
CELL_COUNT_MAX = 16   # PB2A16S = 16 cellules max

# Offsets (en registres word) depuis BASE_RT
OFF_CELL_VOL_0    = 0x00   # UINT16 × 16 (ou 24 ou 32) mV, cellule 0..N
OFF_CELL_STA      = 0x40   # UINT32 – bitmask cellules présentes
OFF_CELL_AVG_VOL  = 0x44   # UINT16 mV
OFF_CELL_DELTA    = 0x46   # UINT16 mV
OFF_MAXMIN_CELL   = 0x48   # UINT8 max | UINT8 min cell number
OFF_MOS_TEMP      = 0x8A   # INT16 × 0.1°C
OFF_BAT_VOL       = 0x90   # UINT32 mV
OFF_BAT_WATT      = 0x94   # UINT32 mW
OFF_BAT_CUR       = 0x98   # INT32 mA
OFF_BAT_TEMP1     = 0x9C   # INT16 × 0.1°C
OFF_BAT_TEMP2     = 0x9E   # INT16 × 0.1°C
OFF_ALARMS        = 0xA0   # UINT32 – bitmask alarmes
OFF_BALAN_CUR     = 0xA4   # INT16 mA
OFF_BALAN_SOC     = 0xA6   # UINT8 balan_state | UINT8 SOC%
OFF_SOC_REMAIN    = 0xA8   # INT32 mAh
OFF_SOC_FULL      = 0xAC   # UINT32 mAh
OFF_CYCLE_COUNT   = 0xB0   # UINT32
OFF_SOH           = 0xB8   # UINT8 SOH% | UINT8 precharge
OFF_RUNTIME       = 0xBC   # UINT32 s
OFF_CHARGE_STA    = 0xC0   # UINT8 charge | UINT8 discharge
OFF_BAT_TEMP3     = 0xF8   # INT16 × 0.1°C
OFF_BAT_TEMP4     = 0xFA   # INT16 × 0.1°C
OFF_BAT_TEMP5     = 0xFC   # INT16 × 0.1°C

# Décalage info (depuis BASE_INFO)
OFF_MODEL         = 0x00   # ASCII 16 octets
OFF_HW_VER        = 0x10   # ASCII  8 octets
OFF_SW_VER        = 0x18   # ASCII  8 octets
OFF_SERIAL        = 0x50   # ASCII 16 octets

# Noms des bits d'alarme
ALARM_NAMES = {
    0:  "Résistance ligne trop élevée",
    1:  "Sur-température MOS",
    2:  "Nombre de cellules incorrect",
    3:  "Capteur courant défaillant",
    4:  "Sur-tension cellule",
    5:  "Sur-tension batterie",
    6:  "Sur-courant charge",
    7:  "Court-circuit charge",
    8:  "Sur-température charge",
    9:  "Sous-température charge",
    10: "Erreur communication interne",
    11: "Sous-tension cellule",
    12: "Sous-tension batterie",
    13: "Sur-courant décharge",
    14: "Court-circuit décharge",
    15: "Sur-température décharge",
    16: "Défaut MOSFET charge",
    17: "Défaut MOSFET décharge",
    18: "GPS déconnecté",
    19: "Modifier le mot de passe",
    20: "Échec démarrage décharge",
    21: "Alarme surchauffe batterie",
}


@dataclass
class JKBMSData:
    """Données complètes d'un JK-BMS."""
    bms_id: int = 1

    # Tensions
    voltage: Optional[float] = None            # V  (tension totale pack)
    cells: dict = field(default_factory=dict)  # {1: 3.312, …} en V
    cell_count: int = 0
    voltage_avg: Optional[float] = None        # V  (tension moyenne cellule)
    voltage_delta: Optional[float] = None      # mV (écart max-min)
    voltage_low: Optional[float] = None        # V  (cellule la plus basse)
    voltage_high: Optional[float] = None       # V  (cellule la plus haute)
    cell_min_num: int = 0
    cell_max_num: int = 0

    # Courant / puissance
    current: Optional[float] = None            # A  (+ charge, - décharge)
    power: Optional[float] = None              # W

    # SoC / SoH
    soc: Optional[float] = None                # %
    soh: Optional[float] = None                # %
    remaining_capacity: Optional[float] = None # Ah
    nominal_capacity: Optional[float] = None   # Ah
    cycle_count: Optional[int] = None
    runtime: Optional[int] = None              # s

    # Températures
    temperature: Optional[float] = None        # °C (sonde 1)
    temperature2: Optional[float] = None       # °C (sonde 2)
    temperature3: Optional[float] = None
    temperature4: Optional[float] = None
    temperature5: Optional[float] = None
    mos_temperature: Optional[float] = None    # °C

    # Balancing
    balance_active: bool = False
    balance_current: Optional[float] = None    # A

    # États
    charge_enabled: bool = False
    discharge_enabled: bool = False
    alarm_bits: int = 0
    alarms: list = field(default_factory=list)

    # Infos statiques (lues une fois)
    model: str = ""
    hw_version: str = ""
    sw_version: str = ""
    serial: str = ""

    online: bool = False


def _regs_to_uint32(regs, offset_words: int) -> int:
    """Lit 2 registres (4 octets) → UINT32 big-endian."""
    hi = regs[offset_words]
    lo = regs[offset_words + 1]
    return (hi << 16) | lo


def _regs_to_int32(regs, offset_words: int) -> int:
    v = _regs_to_uint32(regs, offset_words)
    if v >= 0x80000000:
        v -= 0x100000000
    return v


def _regs_to_int16(regs, offset_words: int) -> int:
    v = regs[offset_words]
    if v >= 0x8000:
        v -= 0x10000
    return v


def _regs_to_ascii(regs, offset_words: int, length_bytes: int) -> str:
    """Lit N registres → chaîne ASCII."""
    out = []
    for i in range(length_bytes // 2):
        w = regs[offset_words + i]
        hi = (w >> 8) & 0xFF
        lo = w & 0xFF
        if hi: out.append(chr(hi))
        if lo: out.append(chr(lo))
    return "".join(out).rstrip('\x00').strip()


def parse_realtime(regs_rt: list, bms_id: int) -> JKBMSData:
    """
    Parse les registres temps réel (base 0x1200).
    regs_rt : liste de registres word (UINT16) lus depuis l'adresse 0x1200.
    On lit 0x115 registres (~280 words) pour couvrir toutes les plages.
    """
    d = JKBMSData(bms_id=bms_id)

    total = len(regs_rt)

    # ── Cellules ──────────────────────────────────────────────
    # Bitmap des cellules présentes (registre 0x40 = word 0x20 = 32)
    cell_bitmap = 0
    if total > 0x21:
        cell_bitmap = _regs_to_uint32(regs_rt, 0x20)  # offset 0x40 bytes = 0x20 words

    # Tensions cellules (mV, UINT16, un par registre)
    cells = {}
    for n in range(CELL_COUNT_MAX):
        if total > n and (cell_bitmap >> n) & 1:
            mv = regs_rt[n]
            if mv > 0:
                cells[n + 1] = round(mv / 1000.0, 3)
    d.cells = cells
    d.cell_count = len(cells)

    if cells:
        d.voltage_low  = min(cells.values())
        d.voltage_high = max(cells.values())
        # Retrouver les numéros
        d.cell_min_num = min(cells, key=cells.get)
        d.cell_max_num = max(cells, key=cells.get)

    # Tension moyenne cellule (word 0x22)
    if total > 0x23:
        d.voltage_avg = round(regs_rt[0x22] / 1000.0, 3)

    # Delta (word 0x23)
    if total > 0x23:
        d.voltage_delta = regs_rt[0x23]  # déjà en mV

    # ── Températures ──────────────────────────────────────────
    # MOS temp : offset 0x8A bytes = 0x45 words
    if total > 0x45:
        d.mos_temperature = round(_regs_to_int16(regs_rt, 0x45) / 10.0, 1)

    # Tension totale : offset 0x90 bytes = 0x48 words
    if total > 0x49:
        d.voltage = round(_regs_to_uint32(regs_rt, 0x48) / 1000.0, 3)

    # Puissance : offset 0x94 bytes = 0x4A words
    if total > 0x4B:
        d.power = round(_regs_to_uint32(regs_rt, 0x4A) / 1000.0, 1)

    # Courant : offset 0x98 bytes = 0x4C words (INT32 mA)
    if total > 0x4D:
        d.current = round(_regs_to_int32(regs_rt, 0x4C) / 1000.0, 3)
        # Recalc puissance signée
        if d.voltage:
            d.power = round(d.voltage * d.current, 1)

    # Temp bat 1 : offset 0x9C = 0x4E words
    if total > 0x4E:
        d.temperature = round(_regs_to_int16(regs_rt, 0x4E) / 10.0, 1)

    # Temp bat 2 : offset 0x9E = 0x4F words
    if total > 0x4F:
        d.temperature2 = round(_regs_to_int16(regs_rt, 0x4F) / 10.0, 1)

    # ── Alarmes ──────────────────────────────────────────────
    # offset 0xA0 = 0x50 words (UINT32)
    if total > 0x51:
        bits = _regs_to_uint32(regs_rt, 0x50)
        d.alarm_bits = bits
        d.alarms = [ALARM_NAMES[i] for i in range(22) if (bits >> i) & 1 and i in ALARM_NAMES]

    # Courant balancing + état (offset 0xA4 = 0x52, INT16 mA)
    if total > 0x52:
        balan_ma = _regs_to_int16(regs_rt, 0x52)
        d.balance_current = round(balan_ma / 1000.0, 3)

    # Balancing state + SOC (offset 0xA6 = 0x53, UINT8|UINT8)
    if total > 0x53:
        w = regs_rt[0x53]
        balan_state = (w >> 8) & 0xFF
        soc_pct     = w & 0xFF
        d.balance_active = balan_state != 0
        d.soc = float(soc_pct)

    # Capacité restante : offset 0xA8 = 0x54 (INT32 mAh)
    if total > 0x55:
        d.remaining_capacity = round(_regs_to_int32(regs_rt, 0x54) / 1000.0, 2)

    # Capacité totale : offset 0xAC = 0x56 (UINT32 mAh)
    if total > 0x57:
        d.nominal_capacity = round(_regs_to_uint32(regs_rt, 0x56) / 1000.0, 2)

    # Cycles : offset 0xB0 = 0x58 (UINT32)
    if total > 0x59:
        d.cycle_count = _regs_to_uint32(regs_rt, 0x58)

    # SOH : offset 0xB8 = 0x5C (UINT8 SOH | UINT8 precharge)
    if total > 0x5C:
        w = regs_rt[0x5C]
        d.soh = float((w >> 8) & 0xFF)

    # Runtime : offset 0xBC = 0x5E (UINT32 s)
    if total > 0x5F:
        d.runtime = _regs_to_uint32(regs_rt, 0x5E)

    # Charge/décharge status : offset 0xC0 = 0x60
    if total > 0x60:
        w = regs_rt[0x60]
        d.charge_enabled    = bool((w >> 8) & 0xFF)
        d.discharge_enabled = bool(w & 0xFF)

    # Temp 3/4/5 : offsets 0xF8/FA/FC = 0x7C/7D/7E
    if total > 0x7C:
        d.temperature3 = round(_regs_to_int16(regs_rt, 0x7C) / 10.0, 1)
    if total > 0x7D:
        d.temperature4 = round(_regs_to_int16(regs_rt, 0x7D) / 10.0, 1)
    if total > 0x7E:
        d.temperature5 = round(_regs_to_int16(regs_rt, 0x7E) / 10.0, 1)

    d.online = True
    return d


def parse_device_info(regs_info: list, d: JKBMSData):
    """Parse les registres d'info statique (base 0x1400)."""
    if len(regs_info) >= 8:
        d.model      = _regs_to_ascii(regs_info, 0, 16)
    if len(regs_info) >= 12:
        d.hw_version = _regs_to_ascii(regs_info, 8, 8)
    if len(regs_info) >= 16:
        d.sw_version = _regs_to_ascii(regs_info, 12, 8)
    if len(regs_info) >= 48:
        d.serial     = _regs_to_ascii(regs_info, 40, 16)


class JKBMSModbusClient:
    """
    Client Modbus RTU pour JK-BMS.
    Supporte deux modes de connexion :
      - TCP  : passerelle RS485/Ethernet (Elfin EE10, EW11, Waveshare, etc.)
      - RTU  : adaptateur USB→RS485 local (/dev/ttyUSB0)
    """

    def __init__(
        self,
        mode: str = "tcp",          # "tcp" | "rtu"
        # Mode TCP
        host: str = "192.168.1.100",
        port: int = 502,
        # Mode RTU
        serial_port: str = "/dev/ttyUSB0",
        baudrate: int = 115200,
        # Commun
        slave_id: int = 1,          # adresse Modbus du BMS (DIP switch)
        timeout: float = 3.0,
    ):
        self.mode        = mode.lower()
        self.host        = host
        self.port        = port
        self.serial_port = serial_port
        self.baudrate    = baudrate
        self.slave_id    = slave_id
        self.timeout     = timeout
        self._client     = None
        self._info_read  = False   # lecture des infos statiques effectuée

    def _make_client(self):
        if self.mode == "tcp":
            # IMPORTANT : la passerelle Elfin EE10/EW11/Waveshare est en mode
            # "transparent" (pass-through TCP↔RS485) — PAS en mode Modbus TCP.
            # On doit donc forcer le framer RTU pour que pymodbus envoie des
            # trames RTU brutes (slave + FC + data + CRC16) via le socket TCP.
            # Le Elfin les transmet telles quelles sur le bus RS485.
            return AsyncModbusTcpClient(
                host=self.host,
                port=self.port,
                timeout=self.timeout,
                framer=FramerType.RTU,
            )
        else:  # rtu
            return AsyncModbusSerialClient(
                port=self.serial_port,
                baudrate=self.baudrate,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=self.timeout,
                framer=FramerType.RTU,
            )

    async def connect(self) -> bool:
        self._client = self._make_client()
        ok = await self._client.connect()
        if ok:
            logger.info("JK-BMS Modbus connecté (%s) slave=%d", self.mode, self.slave_id)
        else:
            logger.error("JK-BMS Modbus connexion échouée (%s)", self.mode)
        return ok

    async def disconnect(self):
        if self._client:
            self._client.close()
            self._client = None

    async def _read_holding(self, start_addr: int, count: int) -> list:
        """Lit `count` registres holding depuis `start_addr`. Retourne liste d'int."""
        if not self._client or not self._client.connected:
            raise ConnectionError("Non connecté")
        resp = await self._client.read_holding_registers(
            address=start_addr, count=count, slave=self.slave_id
        )
        if resp.isError():
            raise ModbusException(f"Erreur lecture registres {hex(start_addr)}: {resp}")
        return list(resp.registers)

    async def poll(self) -> JKBMSData:
        """
        Lit toutes les données utiles du BMS.

        Compatibilité firmware : certains JK-BMS ont une mémoire fragmentée
        avec des zones non-adressables et une limite de ~120 registres par
        requête (testé sur PB2A16S FW≥19). On découpe donc la lecture en
        3 blocs séparés, et on reconstruit un buffer continu de 0x110 words
        attendus par parse_realtime() en remplissant les trous avec des zéros.

        Cartographie observée (Alexis, JK-PB2A16S FW v.7) :
          - 0x1200-0x12A9  (170 regs)  Bloc principal : cellules, stats, V/A/W/SoC, temps 1-2
          - 0x12AA-0x12EF  trou non-adressable
          - 0x12F0-0x12FF  (16 regs)   Temps 3-4-5
          - 0x1400+        (statique)  Serial, modèle, firmware

        Stratégie : 2 lectures de 120 max + 1 lecture de 16 dans le trou.
        """
        # Pré-allocation buffer de 0x110 (272) registres avec des zéros
        BUF_SIZE = 0x110
        regs_rt = [0] * BUF_SIZE

        # Bloc A : 0x1200-0x1277 (offset 0x00-0x77, 120 registres)
        try:
            block_a = await self._read_holding(BASE_RT, 120)
            for i, v in enumerate(block_a):
                regs_rt[i] = v
        except Exception as e:
            logger.debug("BMS %d bloc A (0x1200+120): %s", self.slave_id, e)
            raise  # on ne peut rien faire sans le bloc principal

        # Bloc B : 0x1278-0x12A9 (offset 0x78-0xA9, 50 registres)
        try:
            block_b = await self._read_holding(BASE_RT + 0x78, 50)
            for i, v in enumerate(block_b):
                if 0x78 + i < BUF_SIZE:
                    regs_rt[0x78 + i] = v
        except Exception as e:
            logger.debug("BMS %d bloc B (0x1278+50): %s", self.slave_id, e)
            # Pas critique : on continue sans bloc B (= pas de SoC/voltage/power)

        # Bloc C : 0x12F0-0x12FF (offset 0xF0-0xFF, 16 registres) — temp 3/4/5
        try:
            block_c = await self._read_holding(BASE_RT + 0xF0, 16)
            for i, v in enumerate(block_c):
                if 0xF0 + i < BUF_SIZE:
                    regs_rt[0xF0 + i] = v
        except Exception as e:
            logger.debug("BMS %d bloc C (0x12F0+16): %s", self.slave_id, e)
            # Pas critique : juste pas de temp 3/4/5

        d = parse_realtime(regs_rt, self.slave_id)

        # Infos statiques — une seule lecture au premier poll
        if not self._info_read:
            try:
                regs_info = await self._read_holding(BASE_INFO, 0x50)
                parse_device_info(regs_info, d)
                self._info_read = True
                logger.info("BMS %d — modèle: %s  FW: %s  SN: %s",
                            self.slave_id, d.model, d.sw_version, d.serial)
            except Exception as e:
                logger.debug("Lecture info statique BMS %d: %s", self.slave_id, e)

        return d

    def to_dict(self, d: JKBMSData) -> dict:
        """Convertit en dict unifié pour l'API REST / WebSocket."""
        return {
            "id":                 d.bms_id,
            "type":               "jkbms",
            "voltage":            d.voltage,
            "current":            d.current,
            "power":              d.power,
            "soc":                d.soc,
            "soh":                d.soh,
            "temperature":        d.temperature,
            "temperature2":       d.temperature2,
            "temperature3":       d.temperature3,
            "temperature4":       d.temperature4,
            "temperature5":       d.temperature5,
            "mos_temperature":    d.mos_temperature,
            "voltage_low":        d.voltage_low,
            "voltage_high":       d.voltage_high,
            "voltage_avg":        d.voltage_avg,
            "voltage_delta":      d.voltage_delta,
            "cell_min_num":       d.cell_min_num,
            "cell_max_num":       d.cell_max_num,
            "cell_count":         d.cell_count,
            "cells":              {str(k): v for k, v in sorted(d.cells.items())},
            "remaining_capacity": d.remaining_capacity,
            "nominal_capacity":   d.nominal_capacity,
            "cycle_count":        d.cycle_count,
            "runtime":            d.runtime,
            "balance_active":     d.balance_active,
            "balance_current":    d.balance_current,
            "charge_enabled":     d.charge_enabled,
            "discharge_enabled":  d.discharge_enabled,
            "alarm_bits":         d.alarm_bits,
            "alarm_count":        len(d.alarms),
            "alarms":             d.alarms,
            "model":              d.model,
            "hw_version":         d.hw_version,
            "sw_version":         d.sw_version,
            "serial":             d.serial,
            "online":             d.online,
            "base_state": (
                "Charge"  if d.current and d.current > 0.5  else
                "Dischg"  if d.current and d.current < -0.5 else
                "Idle"
            ) if d.current is not None else None,
            "voltage_state":     "Alarm" if d.alarm_bits & 0x30 else "Normal",
            "current_state":     "Alarm" if d.alarm_bits & 0x00C0 else "Normal",
            "temperature_state": "Alarm" if d.alarm_bits & 0x8300 else "Normal",
        }


class JKBMSFleet:
    """
    Gère plusieurs JK-BMS en parallèle (adresses Modbus 1..N).
    Chaque BMS est interrogé séquentiellement sur le même bus RS485.
    """

    def __init__(
        self,
        mode: str = "tcp",
        host: str = "192.168.1.100",
        port: int = 502,
        serial_port: str = "/dev/ttyUSB0",
        baudrate: int = 115200,
        slave_ids: list = None,
        timeout: float = 3.0,
        poll_interval: int = 30,
    ):
        self.poll_interval = poll_interval
        self._clients: dict[int, JKBMSModbusClient] = {}

        for sid in (slave_ids or [1]):
            self._clients[sid] = JKBMSModbusClient(
                mode=mode, host=host, port=port,
                serial_port=serial_port, baudrate=baudrate,
                slave_id=sid, timeout=timeout,
            )

    async def poll_all(self) -> list:
        """Interroge tous les BMS. Retourne une liste de JKBMSData."""
        results = []
        for sid, client in self._clients.items():
            try:
                if not client._client or not client._client.connected:
                    await client.connect()
                d = await client.poll()
                results.append(d)
                logger.info("BMS %d: %.2fV %.2fA SoC=%.0f%% %s",
                            sid, d.voltage or 0, d.current or 0, d.soc or 0,
                            "⚠" + str(len(d.alarms)) if d.alarms else "OK")
            except Exception as e:
                logger.error("Erreur BMS %d: %s", sid, e)
                try:
                    await client.disconnect()
                except Exception:
                    pass
                # Retourner un objet hors-ligne
                dead = JKBMSData(bms_id=sid, online=False)
                results.append(dead)
        return results
