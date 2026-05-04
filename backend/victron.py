"""
Victron Energy — Modbus TCP Client (standalone, no Home Assistant)
Communication avec le Cerbo GX / Venus OS via Modbus TCP (port 502).

Inspiré de hass-victron (github.com/sfstar/hass-victron) mais adapté pour
une architecture 100% standalone alignée avec les autres modules du projet.

Concepts Victron Modbus :
  - Port Modbus TCP 502 sur le Cerbo GX / VenusGX / CCGX
  - Chaque device physique (MPPT, MultiPlus, BMV…) est accessible via un
    "unit ID" (DIP-switch sur certains, sinon défini dans le Cerbo).
  - Unit ID 100 = Cerbo GX lui-même (registres "system_*")
  - Unit IDs 1-46 = devices branchés (VE.Direct, VE.Bus, VE.Can)
  - Tous les registres sont des Holding Registers (fonction 0x03)
  - Les valeurs sont souvent scalées (÷10, ÷100) selon le registre

Devices supportés (auto-détectés) :
  - system       : Vue globale du Cerbo (flux total, SoC batterie système)
  - vebus        : MultiPlus, Quattro (onduleur/chargeur)
  - solarcharger : SmartSolar MPPT
  - battery      : BMV, SmartShunt, Lynx Smart BMS
  - inverter     : Phoenix Inverter
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

logger = logging.getLogger(__name__)


# ── Compatibilité pymodbus : 3.7 utilise `slave=`, 3.8+ utilise `device_id=` ──
import inspect as _inspect
try:
    _sig = _inspect.signature(AsyncModbusTcpClient.read_holding_registers)
    _params = list(_sig.parameters.keys())
    if "device_id" in _params:
        _UNIT_KWARG = "device_id"
    elif "slave" in _params:
        _UNIT_KWARG = "slave"
    else:
        _UNIT_KWARG = "slave"  # fallback historique
    logger.debug("pymodbus unit kwarg détecté: %s", _UNIT_KWARG)
except Exception:
    _UNIT_KWARG = "slave"


async def _modbus_read(client, address: int, count: int, unit: int):
    """Wrapper pymodbus compatible avec les versions 3.7 et 3.8+."""
    kwargs = {"address": address, "count": count, _UNIT_KWARG: unit}
    return await client.read_holding_registers(**kwargs)


# ── Types de registres ─────────────────────────────────────────────────────
UINT16 = "uint16"
INT16 = "int16"
UINT32 = "uint32"
INT32 = "int32"


@dataclass
class RegisterDef:
    """Définition d'un registre Victron."""
    address: int
    dtype: str = UINT16
    scale: float = 1.0     # valeur réelle = raw / scale
    unit: str = ""
    writable: bool = False


# ═══════════════════════════════════════════════════════════════════════════
#  Définitions des registres (extraits du CCGX Modbus TCP list v3.66)
# ═══════════════════════════════════════════════════════════════════════════

# ── System (Cerbo GX, unit 100) ──────────────────────────────────────────
SYSTEM_REGS = {
    "system_serial":               RegisterDef(800, "string", unit="string"),  # STRING(6)
    # Puissances PV par phase (on-output inverter, on-grid, on-genset)
    "pv_on_output_L1":             RegisterDef(808, UINT16, unit="W"),
    "pv_on_output_L2":             RegisterDef(809, UINT16, unit="W"),
    "pv_on_output_L3":             RegisterDef(810, UINT16, unit="W"),
    "pv_on_grid_L1":               RegisterDef(811, UINT16, unit="W"),
    "pv_on_grid_L2":               RegisterDef(812, UINT16, unit="W"),
    "pv_on_grid_L3":               RegisterDef(813, UINT16, unit="W"),
    "pv_on_genset_L1":             RegisterDef(814, UINT16, unit="W"),
    # Consommation par phase
    "consumption_L1":              RegisterDef(817, UINT16, unit="W"),
    "consumption_L2":              RegisterDef(818, UINT16, unit="W"),
    "consumption_L3":              RegisterDef(819, UINT16, unit="W"),
    # Réseau par phase (signé : + import, - export)
    "grid_L1":                     RegisterDef(820, INT16, unit="W"),
    "grid_L2":                     RegisterDef(821, INT16, unit="W"),
    "grid_L3":                     RegisterDef(822, INT16, unit="W"),
    # Generator
    "genset_L1":                   RegisterDef(823, INT16, unit="W"),
    "genset_L2":                   RegisterDef(824, INT16, unit="W"),
    "genset_L3":                   RegisterDef(825, INT16, unit="W"),
    # Source active
    "active_input_source":         RegisterDef(826, INT16),
    # Batterie système
    "battery_voltage":             RegisterDef(840, UINT16, 10.0, "V"),
    "battery_current":             RegisterDef(841, INT16, 10.0, "A"),
    "battery_power":               RegisterDef(842, INT16, unit="W"),
    "battery_soc":                 RegisterDef(843, UINT16, 1.0, "%"),
    "battery_state":               RegisterDef(844, UINT16),  # 0=Idle 1=Charge 2=Discharge
    "battery_time_to_go":          RegisterDef(846, UINT16, 0.01, "s"),
    # DC total
    "dc_pv_power":                 RegisterDef(850, UINT16, unit="W"),
    "dc_pv_current":               RegisterDef(851, INT16, 10.0, "A"),
    # Charger / système
    "charger_power":               RegisterDef(855, UINT16, unit="W"),
    "system_power":                RegisterDef(860, INT16, unit="W"),
}

# ── VE.Bus (MultiPlus / Quattro, unit 227 typiquement) ───────────────────
VEBUS_REGS = {
    # AC-IN (activein)
    "ac_in_L1_voltage":            RegisterDef(3, UINT16, 10.0, "V"),
    "ac_in_L2_voltage":            RegisterDef(4, UINT16, 10.0, "V"),
    "ac_in_L3_voltage":            RegisterDef(5, UINT16, 10.0, "V"),
    "ac_in_L1_current":            RegisterDef(6, INT16, 10.0, "A"),
    "ac_in_L2_current":            RegisterDef(7, INT16, 10.0, "A"),
    "ac_in_L3_current":            RegisterDef(8, INT16, 10.0, "A"),
    "ac_in_L1_frequency":          RegisterDef(9, INT16, 100.0, "Hz"),
    "ac_in_L1_power":              RegisterDef(12, INT16, 0.1, "W"),
    "ac_in_L2_power":              RegisterDef(13, INT16, 0.1, "W"),
    "ac_in_L3_power":              RegisterDef(14, INT16, 0.1, "W"),
    # AC-OUT
    "ac_out_L1_voltage":           RegisterDef(15, UINT16, 10.0, "V"),
    "ac_out_L2_voltage":           RegisterDef(16, UINT16, 10.0, "V"),
    "ac_out_L3_voltage":           RegisterDef(17, UINT16, 10.0, "V"),
    "ac_out_L1_current":           RegisterDef(18, INT16, 10.0, "A"),
    "ac_out_L2_current":           RegisterDef(19, INT16, 10.0, "A"),
    "ac_out_L3_current":           RegisterDef(20, INT16, 10.0, "A"),
    "ac_out_L1_frequency":         RegisterDef(21, INT16, 100.0, "Hz"),
    "ac_in_current_limit":         RegisterDef(22, INT16, 10.0, "A", writable=True),
    "ac_out_L1_power":             RegisterDef(23, INT16, 0.1, "W"),
    "ac_out_L2_power":             RegisterDef(24, INT16, 0.1, "W"),
    "ac_out_L3_power":             RegisterDef(25, INT16, 0.1, "W"),
    # Batterie
    "battery_voltage":             RegisterDef(26, UINT16, 100.0, "V"),
    "battery_current":             RegisterDef(27, INT16, 10.0, "A"),
    "number_of_phases":            RegisterDef(28, UINT16),
    "active_input":                RegisterDef(29, UINT16),  # 0=AC1 1=AC2 240=Disconnected
    "soc":                         RegisterDef(30, UINT16, 10.0, "%"),
    "state":                       RegisterDef(31, UINT16),  # charger_state
    "error":                       RegisterDef(32, UINT16),
    "mode":                        RegisterDef(33, UINT16, writable=True),  # 1=Chg 2=Inv 3=On 4=Off
    # Alarmes
    "alarm_high_temp":             RegisterDef(34, UINT16),
    "alarm_low_battery":           RegisterDef(35, UINT16),
    "alarm_overload":              RegisterDef(36, UINT16),
    # Setpoints
    "ac_setpoint_L1":              RegisterDef(37, INT16, unit="W", writable=True),
    "disable_charge":              RegisterDef(38, UINT16, writable=True),
    "disable_feedin":              RegisterDef(39, UINT16, writable=True),
}

# ── Solar Charger (SmartSolar MPPT, unit 1-4 typiquement) ────────────────
SOLARCHARGER_REGS = {
    "battery_voltage":             RegisterDef(771, UINT16, 100.0, "V"),
    "battery_current":             RegisterDef(772, INT16, 10.0, "A"),
    "battery_temperature":         RegisterDef(773, INT16, 10.0, "°C"),
    "mode":                        RegisterDef(774, UINT16, writable=True),
    "state":                       RegisterDef(775, UINT16),  # 0=Off 2=Fault 3=Bulk 4=Abs 5=Float…
    "pv_voltage":                  RegisterDef(776, UINT16, 100.0, "V"),
    "pv_current":                  RegisterDef(777, INT16, 10.0, "A"),
    "equalization_pending":        RegisterDef(778, UINT16),
    "equalization_time_remaining": RegisterDef(779, UINT16, 10.0, "s"),
    "relay":                       RegisterDef(780, UINT16),
    "alarm":                       RegisterDef(781, UINT16),
    "alarm_low_voltage":           RegisterDef(782, UINT16),
    "alarm_high_voltage":          RegisterDef(783, UINT16),
    "yield_today":                 RegisterDef(784, UINT16, 10.0, "kWh"),
    "max_power_today":             RegisterDef(785, UINT16, unit="W"),
    "yield_yesterday":             RegisterDef(786, UINT16, 10.0, "kWh"),
    "max_power_yesterday":         RegisterDef(787, UINT16, unit="W"),
    "errorcode":                   RegisterDef(788, UINT16),
    "pv_power":                    RegisterDef(789, UINT16, 10.0, "W"),
    "yield_user":                  RegisterDef(790, UINT16, 10.0, "kWh"),
    "mpp_operation_mode":          RegisterDef(791, UINT16),
}

# ── Battery Monitor (BMV, SmartShunt, Lynx BMS, unit 225 typiquement) ────
BATTERY_REGS = {
    "power":                       RegisterDef(258, INT16, unit="W", writable=True),
    "voltage":                     RegisterDef(259, UINT16, 100.0, "V"),
    "starter_voltage":             RegisterDef(260, UINT16, 100.0, "V"),
    "current":                     RegisterDef(261, INT16, 10.0, "A"),
    "temperature":                 RegisterDef(262, INT16, 10.0, "°C"),
    "mid_voltage":                 RegisterDef(263, UINT16, 100.0, "V"),
    "mid_voltage_deviation":       RegisterDef(264, UINT16, 100.0, "%"),
    "consumed_amphours":           RegisterDef(265, UINT16, -10.0, "Ah"),
    "soc":                         RegisterDef(266, UINT16, 10.0, "%"),
    "alarm":                       RegisterDef(267, UINT16),
    "time_to_go":                  RegisterDef(303, UINT16, 0.01, "s"),
    "history_deepest_discharge":   RegisterDef(281, UINT16, -10.0, "Ah"),
    "history_charge_cycles":       RegisterDef(284, UINT16),
    "history_full_discharges":     RegisterDef(285, UINT16),
    "history_total_ah_drawn":      RegisterDef(286, INT32, -10.0, "Ah"),
}


# ═══════════════════════════════════════════════════════════════════════════
#  Enums (état humain-lisible)
# ═══════════════════════════════════════════════════════════════════════════

CHARGER_STATE = {
    0: "Off", 1: "Low power", 2: "Fault", 3: "Bulk", 4: "Absorption",
    5: "Float", 6: "Storage", 7: "Equalize", 8: "Passthru", 9: "Inverting",
    10: "Power assist", 11: "Power supply", 244: "Sustain", 252: "External control",
}

VEBUS_MODE = {1: "Charger only", 2: "Inverter only", 3: "On", 4: "Off"}
VEBUS_ACTIVE_INPUT = {0: "AC input 1", 1: "AC input 2", 240: "Disconnected"}

SYSTEM_BATTERY_STATE = {0: "Idle", 1: "Charging", 2: "Discharging"}

ALARM_STATE = {0: "OK", 1: "Warning", 2: "Alarm"}


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _decode_uint16(regs: list, offset: int) -> int:
    return regs[offset] & 0xFFFF


def _decode_int16(regs: list, offset: int) -> int:
    v = regs[offset] & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _decode_uint32(regs: list, offset: int) -> int:
    # Victron : big-endian (MSB first)
    return ((regs[offset] & 0xFFFF) << 16) | (regs[offset + 1] & 0xFFFF)


def _decode_int32(regs: list, offset: int) -> int:
    v = _decode_uint32(regs, offset)
    return v - 0x100000000 if v >= 0x80000000 else v


def _decode_string(regs: list, offset: int, length_words: int) -> str:
    out = []
    for i in range(length_words):
        w = regs[offset + i]
        hi = (w >> 8) & 0xFF
        lo = w & 0xFF
        if hi: out.append(chr(hi))
        if lo: out.append(chr(lo))
    return "".join(out).rstrip('\x00').strip()


def _scale(raw: int, scale: float) -> float:
    """Applique le scaling Victron : valeur réelle = raw / scale."""
    if scale == 0 or scale == 1:
        return float(raw)
    return round(raw / scale, 3)


def _decode_register(regs: list, offset: int, reg_def: RegisterDef):
    """Décode un registre selon sa définition."""
    try:
        if reg_def.dtype == UINT16:
            raw = _decode_uint16(regs, offset)
        elif reg_def.dtype == INT16:
            raw = _decode_int16(regs, offset)
        elif reg_def.dtype == UINT32:
            raw = _decode_uint32(regs, offset)
        elif reg_def.dtype == INT32:
            raw = _decode_int32(regs, offset)
        else:
            return None
        return _scale(raw, reg_def.scale)
    except (IndexError, TypeError):
        return None


def _count_registers(reg_def: RegisterDef) -> int:
    """Nombre de registres 16-bit pour un type donné."""
    if reg_def.dtype in (UINT16, INT16):
        return 1
    if reg_def.dtype in (UINT32, INT32):
        return 2
    if reg_def.dtype == "string":
        return 6  # STRING(6) pour le serial
    return 1


# ═══════════════════════════════════════════════════════════════════════════
#  Data classes
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class VictronSystemData:
    """Vue système globale (Cerbo GX, unit 100)."""
    unit_id: int = 100
    serial: str = ""

    # Puissances par phase
    pv_on_output: int = 0
    pv_on_grid: int = 0
    pv_on_genset: int = 0
    consumption: int = 0
    grid: int = 0
    genset: int = 0

    # Batterie système
    battery_voltage: Optional[float] = None
    battery_current: Optional[float] = None
    battery_power: Optional[int] = None
    battery_soc: Optional[float] = None
    battery_state: str = ""
    battery_time_to_go: Optional[int] = None  # secondes

    # DC
    dc_pv_power: Optional[int] = None
    dc_pv_current: Optional[float] = None

    # Système
    charger_power: Optional[int] = None
    system_power: Optional[int] = None
    active_input_source: int = 0

    online: bool = False


@dataclass
class VictronVebusData:
    """MultiPlus / Quattro (VE.Bus)."""
    unit_id: int = 0
    device_type: str = "vebus"

    # AC-IN
    ac_in_voltage: Optional[float] = None
    ac_in_current: Optional[float] = None
    ac_in_frequency: Optional[float] = None
    ac_in_power: int = 0
    ac_in_current_limit: Optional[float] = None

    # AC-OUT
    ac_out_voltage: Optional[float] = None
    ac_out_current: Optional[float] = None
    ac_out_frequency: Optional[float] = None
    ac_out_power: int = 0

    # Batterie
    battery_voltage: Optional[float] = None
    battery_current: Optional[float] = None
    soc: Optional[float] = None

    # État
    state: str = ""           # Bulk, Absorption, Float…
    mode: str = ""            # Charger only, Inverter only, On, Off
    active_input: str = ""
    error: int = 0
    num_phases: int = 1

    # Alarmes
    alarm_high_temp: str = "OK"
    alarm_low_battery: str = "OK"
    alarm_overload: str = "OK"

    online: bool = False


@dataclass
class VictronSolarChargerData:
    """SmartSolar MPPT."""
    unit_id: int = 0
    device_type: str = "solarcharger"

    battery_voltage: Optional[float] = None
    battery_current: Optional[float] = None
    battery_temperature: Optional[float] = None

    pv_voltage: Optional[float] = None
    pv_current: Optional[float] = None
    pv_power: Optional[int] = None

    state: str = ""              # Off, Bulk, Absorption…
    mpp_operation_mode: int = 0

    yield_today: Optional[float] = None       # kWh
    yield_yesterday: Optional[float] = None   # kWh
    yield_user: Optional[float] = None        # kWh (total depuis reset)
    max_power_today: Optional[int] = None
    max_power_yesterday: Optional[int] = None

    errorcode: int = 0
    alarm: str = "OK"
    alarm_low_voltage: str = "OK"
    alarm_high_voltage: str = "OK"

    online: bool = False


@dataclass
class VictronBatteryData:
    """BMV / SmartShunt / Lynx BMS."""
    unit_id: int = 0
    device_type: str = "battery"

    voltage: Optional[float] = None
    current: Optional[float] = None
    power: Optional[int] = None
    soc: Optional[float] = None
    temperature: Optional[float] = None
    starter_voltage: Optional[float] = None
    mid_voltage: Optional[float] = None
    mid_voltage_deviation: Optional[float] = None

    consumed_amphours: Optional[float] = None
    time_to_go: Optional[int] = None  # secondes

    history_deepest_discharge: Optional[float] = None
    history_charge_cycles: Optional[int] = None
    history_full_discharges: Optional[int] = None
    history_total_ah_drawn: Optional[float] = None

    alarm: str = "OK"

    online: bool = False


# ═══════════════════════════════════════════════════════════════════════════
#  Client Modbus TCP
# ═══════════════════════════════════════════════════════════════════════════

class VictronModbusClient:
    """
    Client Modbus TCP unifié pour Cerbo GX / Venus OS.

    Utilisation :
        client = VictronModbusClient(host="192.168.1.50", port=502)
        await client.connect()
        devices = await client.scan_devices()   # auto-détection
        snapshot = await client.poll_all()
        await client.disconnect()
    """

    # Unit IDs à scanner (plage Victron standard)
    # 0-46 = devices VE.Direct/VE.Bus/VE.Can
    # 100 = Cerbo GX system
    # 225-247 = BMV, SmartShunt, Lynx BMS, onduleurs secondaires
    SCAN_UNIT_IDS = (
        list(range(1, 47))      # devices principaux
        + [100]                 # Cerbo GX system
        + list(range(220, 248)) # BMV / Lynx / Multi secondaires
    )

    def __init__(self, host: str, port: int = 502, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._client: Optional[AsyncModbusTcpClient] = None
        self._lock = asyncio.Lock()

        # Résultat du scan : {unit_id: "vebus"|"solarcharger"|"battery"|"system"}
        self.discovered: dict[int, str] = {}

    async def connect(self) -> bool:
        self._client = AsyncModbusTcpClient(host=self.host, port=self.port, timeout=self.timeout)
        ok = await self._client.connect()
        if ok:
            # Petit délai pour laisser le socket se stabiliser
            await asyncio.sleep(0.3)
            # Lecture de warmup sur unit 100 reg 800 (serial Cerbo, toujours présent)
            try:
                resp = await _modbus_read(self._client, address=800, count=1, unit=100)
                if resp.isError():
                    logger.warning("Warmup read échoué : %s", resp)
                else:
                    logger.info("Victron Modbus TCP connecté %s:%d (warmup OK, kwarg=%s)",
                                self.host, self.port, _UNIT_KWARG)
            except Exception as e:
                logger.warning("Warmup read exception: %s (%s)", e, type(e).__name__)
                logger.info("Victron Modbus TCP connecté %s:%d (warmup failed)",
                            self.host, self.port)
        else:
            logger.error("Victron Modbus TCP échec %s:%d", self.host, self.port)
        return ok

    async def disconnect(self):
        if self._client:
            self._client.close()
            self._client = None

    async def _read_regs(self, unit_id: int, address: int, count: int) -> Optional[list]:
        """Lit `count` registres holding depuis `address` pour `unit_id`."""
        if not self._client or not self._client.connected:
            return None

        try:
            resp = await _modbus_read(self._client, address=address, count=count, unit=unit_id)
            if resp.isError():
                return None
            return list(resp.registers)
        except (ModbusException, asyncio.TimeoutError) as e:
            logger.debug("Read unit=%d addr=%d count=%d: %s",
                         unit_id, address, count, e)
            return None
        except Exception as e:
            logger.debug("Read unit=%d addr=%d count=%d unexpected: %s (%s)",
                         unit_id, address, count, e, type(e).__name__)
            return None

    async def _probe_unit(self, unit_id: int) -> Optional[str]:
        """
        Teste un unit ID et détermine son type de device.
        Retourne le type ('vebus'|'solarcharger'|'battery'|'system') ou None.
        """
        # Unit 100 = Cerbo GX system
        if unit_id == 100:
            # Tester system_battery_voltage (840)
            regs = await self._read_regs(unit_id, 840, 2)
            if regs is not None:
                return "system"
            return None

        # Tester vebus (reg 3 = ac_in_L1_voltage)
        regs = await self._read_regs(unit_id, 3, 1)
        if regs is not None:
            # Confirmer avec reg 31 (state) qui doit être valide
            regs2 = await self._read_regs(unit_id, 30, 4)
            if regs2 is not None:
                return "vebus"

        # Tester solarcharger (reg 771 = battery_voltage)
        regs = await self._read_regs(unit_id, 771, 2)
        if regs is not None:
            return "solarcharger"

        # Tester battery monitor (reg 259 = voltage)
        regs = await self._read_regs(unit_id, 259, 2)
        if regs is not None:
            return "battery"

        return None

    async def scan_devices(self, unit_ids: list = None) -> dict:
        """
        Auto-détecte les devices présents.
        Peut prendre 10-30s selon le nombre d'unit IDs à tester.
        """
        ids = unit_ids if unit_ids is not None else self.SCAN_UNIT_IDS
        logger.info("Scan Victron : test de %d unit IDs sur %s:%d…",
                    len(ids), self.host, self.port)

        # S'assurer que la connexion est vivante avant de scanner
        if not self._client or not self._client.connected:
            logger.warning("Scan: client non connecté, reconnexion…")
            await self.connect()
            if not self._client or not self._client.connected:
                logger.error("Scan: impossible de se connecter à %s:%d",
                             self.host, self.port)
                return {}

        discovered = {}
        errors = 0
        async with self._lock:
            for uid in ids:
                try:
                    dev_type = await self._probe_unit(uid)
                    if dev_type:
                        discovered[uid] = dev_type
                        logger.info("  ✓ Unit %d = %s", uid, dev_type)
                except Exception as e:
                    errors += 1
                    logger.debug("Probe unit %d: %s (%s)", uid, e, type(e).__name__)

        self.discovered = discovered
        logger.info("Scan terminé : %d devices détectés (%d erreurs sur %d tests)",
                    len(discovered), errors, len(ids))
        if len(discovered) == 0 and errors == 0:
            logger.warning(
                "⚠️  Aucun device détecté mais aucune erreur — vérifier que "
                "Modbus TCP est activé sur le Cerbo (Settings → Services → "
                "Modbus TCP → ON) et que le firewall n'interfère pas."
            )
        return discovered

    async def read_system(self, unit_id: int = 100) -> VictronSystemData:
        """Lit la vue système globale du Cerbo GX."""
        data = VictronSystemData(unit_id=unit_id)

        # Serial (800, 6 words)
        regs = await self._read_regs(unit_id, 800, 6)
        if regs:
            data.serial = _decode_string(regs, 0, 6)

        # Puissances (808-825)
        regs = await self._read_regs(unit_id, 808, 20)
        if regs:
            data.pv_on_output = (_decode_uint16(regs, 0) + _decode_uint16(regs, 1)
                                 + _decode_uint16(regs, 2))
            data.pv_on_grid = (_decode_uint16(regs, 3) + _decode_uint16(regs, 4)
                               + _decode_uint16(regs, 5))
            data.pv_on_genset = (_decode_uint16(regs, 6) + _decode_uint16(regs, 7)
                                 + _decode_uint16(regs, 8))
            data.consumption = (_decode_uint16(regs, 9) + _decode_uint16(regs, 10)
                                + _decode_uint16(regs, 11))
            data.grid = (_decode_int16(regs, 12) + _decode_int16(regs, 13)
                         + _decode_int16(regs, 14))
            data.genset = (_decode_int16(regs, 15) + _decode_int16(regs, 16)
                           + _decode_int16(regs, 17))
            data.active_input_source = _decode_int16(regs, 18)

        # Batterie système (840-846)
        regs = await self._read_regs(unit_id, 840, 7)
        if regs:
            data.battery_voltage = _scale(_decode_uint16(regs, 0), 10.0)
            data.battery_current = _scale(_decode_int16(regs, 1), 10.0)
            data.battery_power = _decode_int16(regs, 2)
            data.battery_soc = _scale(_decode_uint16(regs, 3), 1.0)
            state_raw = _decode_uint16(regs, 4)
            data.battery_state = SYSTEM_BATTERY_STATE.get(state_raw, f"Unknown({state_raw})")
            # reg 845 = amphours, reg 846 = time_to_go
            if len(regs) >= 7:
                tg = _decode_uint16(regs, 6)
                data.battery_time_to_go = int(tg / 0.01) if tg else None

        # DC PV (850-851)
        regs = await self._read_regs(unit_id, 850, 2)
        if regs:
            data.dc_pv_power = _decode_uint16(regs, 0)
            data.dc_pv_current = _scale(_decode_int16(regs, 1), 10.0)

        # Charger power (855) + system power (860)
        regs = await self._read_regs(unit_id, 855, 1)
        if regs:
            data.charger_power = _decode_uint16(regs, 0)
        regs = await self._read_regs(unit_id, 860, 1)
        if regs:
            data.system_power = _decode_int16(regs, 0)

        data.online = True
        return data

    async def read_vebus(self, unit_id: int) -> VictronVebusData:
        """Lit un device VE.Bus (MultiPlus/Quattro)."""
        data = VictronVebusData(unit_id=unit_id)

        # Bloc principal 3-36 (34 registres)
        regs = await self._read_regs(unit_id, 3, 34)
        if regs is None:
            return data

        # AC-IN (sommer les 3 phases)
        try:
            data.ac_in_voltage = _scale(_decode_uint16(regs, 0), 10.0)
            l1_i = _scale(_decode_int16(regs, 3), 10.0)
            l2_i = _scale(_decode_int16(regs, 4), 10.0)
            l3_i = _scale(_decode_int16(regs, 5), 10.0)
            data.ac_in_current = round(l1_i + l2_i + l3_i, 2)
            data.ac_in_frequency = _scale(_decode_int16(regs, 6), 100.0)

            l1_p = _scale(_decode_int16(regs, 9), 0.1)
            l2_p = _scale(_decode_int16(regs, 10), 0.1)
            l3_p = _scale(_decode_int16(regs, 11), 0.1)
            data.ac_in_power = int(l1_p + l2_p + l3_p)

            # AC-OUT
            data.ac_out_voltage = _scale(_decode_uint16(regs, 12), 10.0)
            o1_i = _scale(_decode_int16(regs, 15), 10.0)
            o2_i = _scale(_decode_int16(regs, 16), 10.0)
            o3_i = _scale(_decode_int16(regs, 17), 10.0)
            data.ac_out_current = round(o1_i + o2_i + o3_i, 2)
            data.ac_out_frequency = _scale(_decode_int16(regs, 18), 100.0)
            data.ac_in_current_limit = _scale(_decode_int16(regs, 19), 10.0)

            o1_p = _scale(_decode_int16(regs, 20), 0.1)
            o2_p = _scale(_decode_int16(regs, 21), 0.1)
            o3_p = _scale(_decode_int16(regs, 22), 0.1)
            data.ac_out_power = int(o1_p + o2_p + o3_p)

            # Batterie (reg 26-27)
            data.battery_voltage = _scale(_decode_uint16(regs, 23), 100.0)
            data.battery_current = _scale(_decode_int16(regs, 24), 10.0)
            data.num_phases = _decode_uint16(regs, 25)

            # État (reg 29-36)
            active_in = _decode_uint16(regs, 26)
            data.active_input = VEBUS_ACTIVE_INPUT.get(active_in, f"Unknown({active_in})")
            data.soc = _scale(_decode_uint16(regs, 27), 10.0)
            state_raw = _decode_uint16(regs, 28)
            data.state = CHARGER_STATE.get(state_raw, f"State({state_raw})")
            data.error = _decode_uint16(regs, 29)
            mode_raw = _decode_uint16(regs, 30)
            data.mode = VEBUS_MODE.get(mode_raw, f"Mode({mode_raw})")
            data.alarm_high_temp = ALARM_STATE.get(_decode_uint16(regs, 31), "Unknown")
            data.alarm_low_battery = ALARM_STATE.get(_decode_uint16(regs, 32), "Unknown")
            data.alarm_overload = ALARM_STATE.get(_decode_uint16(regs, 33), "Unknown")

        except (IndexError, KeyError) as e:
            logger.debug("vebus decode error unit=%d: %s", unit_id, e)

        data.online = True
        return data

    async def read_solarcharger(self, unit_id: int) -> VictronSolarChargerData:
        """Lit un MPPT SmartSolar."""
        data = VictronSolarChargerData(unit_id=unit_id)

        regs = await self._read_regs(unit_id, 771, 21)
        if regs is None:
            return data

        try:
            data.battery_voltage = _scale(_decode_uint16(regs, 0), 100.0)
            data.battery_current = _scale(_decode_int16(regs, 1), 10.0)
            data.battery_temperature = _scale(_decode_int16(regs, 2), 10.0)
            state_raw = _decode_uint16(regs, 4)
            data.state = CHARGER_STATE.get(state_raw, f"State({state_raw})")
            data.pv_voltage = _scale(_decode_uint16(regs, 5), 100.0)
            data.pv_current = _scale(_decode_int16(regs, 6), 10.0)
            data.alarm = ALARM_STATE.get(_decode_uint16(regs, 10), "Unknown")
            data.alarm_low_voltage = ALARM_STATE.get(_decode_uint16(regs, 11), "Unknown")
            data.alarm_high_voltage = ALARM_STATE.get(_decode_uint16(regs, 12), "Unknown")
            data.yield_today = _scale(_decode_uint16(regs, 13), 10.0)
            data.max_power_today = _decode_uint16(regs, 14)
            data.yield_yesterday = _scale(_decode_uint16(regs, 15), 10.0)
            data.max_power_yesterday = _decode_uint16(regs, 16)
            data.errorcode = _decode_uint16(regs, 17)
            data.pv_power = int(_scale(_decode_uint16(regs, 18), 10.0))
            data.yield_user = _scale(_decode_uint16(regs, 19), 10.0)
            data.mpp_operation_mode = _decode_uint16(regs, 20)
        except (IndexError, KeyError) as e:
            logger.debug("solarcharger decode error unit=%d: %s", unit_id, e)

        data.online = True
        return data

    async def read_battery(self, unit_id: int) -> VictronBatteryData:
        """Lit un Battery Monitor (BMV, SmartShunt, Lynx BMS)."""
        data = VictronBatteryData(unit_id=unit_id)

        # Bloc 258-289
        regs = await self._read_regs(unit_id, 258, 30)
        if regs is None:
            return data

        try:
            data.power = _decode_int16(regs, 0)                    # 258
            data.voltage = _scale(_decode_uint16(regs, 1), 100.0)  # 259
            data.starter_voltage = _scale(_decode_uint16(regs, 2), 100.0)  # 260
            data.current = _scale(_decode_int16(regs, 3), 10.0)    # 261
            data.temperature = _scale(_decode_int16(regs, 4), 10.0)  # 262
            data.mid_voltage = _scale(_decode_uint16(regs, 5), 100.0)  # 263
            data.mid_voltage_deviation = _scale(_decode_uint16(regs, 6), 100.0)  # 264
            data.consumed_amphours = _scale(_decode_uint16(regs, 7), -10.0)  # 265
            data.soc = _scale(_decode_uint16(regs, 8), 10.0)       # 266
            data.alarm = ALARM_STATE.get(_decode_uint16(regs, 9), "Unknown")  # 267

            # Historique (281-289)
            if len(regs) >= 28:
                data.history_deepest_discharge = _scale(_decode_uint16(regs, 23), -10.0)
                data.history_charge_cycles = _decode_uint16(regs, 26)
                data.history_full_discharges = _decode_uint16(regs, 27)

        except (IndexError, KeyError) as e:
            logger.debug("battery decode error unit=%d: %s", unit_id, e)

        # Time to go (reg 303)
        regs_tg = await self._read_regs(unit_id, 303, 1)
        if regs_tg:
            tg = _decode_uint16(regs_tg, 0)
            data.time_to_go = int(tg / 0.01) if tg else None

        data.online = True
        return data

    async def poll_all(self) -> dict:
        """
        Interroge tous les devices découverts.
        Retourne : {
            "system": VictronSystemData,
            "vebus":  {unit_id: VictronVebusData, …},
            "solarchargers": {unit_id: VictronSolarChargerData, …},
            "batteries": {unit_id: VictronBatteryData, …},
        }
        """
        if not self.discovered:
            await self.scan_devices()

        result = {
            "system": None,
            "vebus": {},
            "solarchargers": {},
            "batteries": {},
        }

        async with self._lock:
            for uid, dtype in self.discovered.items():
                try:
                    if dtype == "system":
                        result["system"] = await self.read_system(uid)
                    elif dtype == "vebus":
                        result["vebus"][uid] = await self.read_vebus(uid)
                    elif dtype == "solarcharger":
                        result["solarchargers"][uid] = await self.read_solarcharger(uid)
                    elif dtype == "battery":
                        result["batteries"][uid] = await self.read_battery(uid)
                except Exception as e:
                    logger.error("Poll unit %d (%s): %s", uid, dtype, e)

        return result


# ═══════════════════════════════════════════════════════════════════════════
#  Conversion → dict unifié pour l'API
# ═══════════════════════════════════════════════════════════════════════════

def system_to_dict(d: VictronSystemData) -> dict:
    return {
        "id": d.unit_id,
        "type": "victron_system",
        "serial": d.serial,
        "pv_on_output_power": d.pv_on_output,
        "pv_on_grid_power": d.pv_on_grid,
        "pv_on_genset_power": d.pv_on_genset,
        "dc_pv_power": d.dc_pv_power or 0,
        "total_pv_power": (d.pv_on_output or 0) + (d.pv_on_grid or 0)
                          + (d.pv_on_genset or 0) + (d.dc_pv_power or 0),
        "consumption_power": d.consumption,
        "grid_power": d.grid,
        "genset_power": d.genset,
        "battery_voltage": d.battery_voltage,
        "battery_current": d.battery_current,
        "battery_power": d.battery_power,
        "battery_soc": d.battery_soc,
        "battery_state": d.battery_state,
        "battery_time_to_go": d.battery_time_to_go,
        "charger_power": d.charger_power,
        "system_power": d.system_power,
        "active_input_source": d.active_input_source,
        "online": d.online,
    }


def vebus_to_dict(d: VictronVebusData) -> dict:
    """Dict unifié compatible avec le format onduleur (Voltronic-like)."""
    return {
        "id": d.unit_id,
        "type": "victron_vebus",
        # Format compatible onduleur
        "grid_voltage": d.ac_in_voltage,
        "grid_frequency": d.ac_in_frequency,
        "output_voltage": d.ac_out_voltage,
        "output_frequency": d.ac_out_frequency,
        "output_active_power": d.ac_out_power,
        "output_apparent_power": d.ac_out_power,  # approx
        "output_load_percent": None,
        "battery_voltage": d.battery_voltage,
        "battery_capacity": d.soc,
        "battery_charge_current": d.battery_current if (d.battery_current or 0) > 0 else 0,
        "battery_discharge_current": -d.battery_current if (d.battery_current or 0) < 0 else 0,
        "battery_power": int((d.battery_voltage or 0) * (d.battery_current or 0)),
        # Spécifique Victron
        "ac_in_voltage": d.ac_in_voltage,
        "ac_in_current": d.ac_in_current,
        "ac_in_power": d.ac_in_power,
        "ac_in_current_limit": d.ac_in_current_limit,
        "ac_out_voltage": d.ac_out_voltage,
        "ac_out_current": d.ac_out_current,
        "ac_out_power": d.ac_out_power,
        "num_phases": d.num_phases,
        "active_input": d.active_input,
        "mode": d.mode,
        "state": d.state,
        "error": d.error,
        "alarm_high_temp": d.alarm_high_temp,
        "alarm_low_battery": d.alarm_low_battery,
        "alarm_overload": d.alarm_overload,
        "pv_input_power": 0,  # Le PV vient des solarchargers, pas du VE.Bus
        "inverter_temperature": None,
        "online": d.online,
    }


def solarcharger_to_dict(d: VictronSolarChargerData) -> dict:
    return {
        "id": d.unit_id,
        "type": "victron_solarcharger",
        "battery_voltage": d.battery_voltage,
        "battery_current": d.battery_current,
        "battery_temperature": d.battery_temperature,
        "pv_voltage": d.pv_voltage,
        "pv_current": d.pv_current,
        "pv_power": d.pv_power,
        "state": d.state,
        "yield_today_kwh": d.yield_today,
        "yield_yesterday_kwh": d.yield_yesterday,
        "yield_user_kwh": d.yield_user,
        "max_power_today": d.max_power_today,
        "max_power_yesterday": d.max_power_yesterday,
        "mpp_operation_mode": d.mpp_operation_mode,
        "errorcode": d.errorcode,
        "alarm": d.alarm,
        "online": d.online,
    }


def battery_to_dict(d: VictronBatteryData) -> dict:
    """Dict unifié compatible avec le format BMS."""
    return {
        "id": d.unit_id,
        "type": "victron_battery",
        "voltage": d.voltage,
        "current": d.current,
        "power": d.power,
        "soc": d.soc,
        "temperature": d.temperature,
        "mid_voltage": d.mid_voltage,
        "mid_voltage_deviation": d.mid_voltage_deviation,
        "consumed_amphours": d.consumed_amphours,
        "time_to_go": d.time_to_go,
        "cycle_count": d.history_charge_cycles,
        "history_deepest_discharge": d.history_deepest_discharge,
        "history_full_discharges": d.history_full_discharges,
        "alarm": d.alarm,
        "alarm_count": 1 if d.alarm and d.alarm != "OK" else 0,
        "alarms": [f"Alarme: {d.alarm}"] if d.alarm and d.alarm != "OK" else [],
        "base_state": (
            "Charge" if d.current and d.current > 0.5 else
            "Dischg" if d.current and d.current < -0.5 else
            "Idle"
        ) if d.current is not None else None,
        "cells": {},  # Victron ne donne pas les cellules individuelles
        "online": d.online,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  Fleet (équivalent aux autres modules)
# ═══════════════════════════════════════════════════════════════════════════

class VictronFleet:
    """
    Gère un Cerbo GX et tous ses sous-devices via une unique connexion Modbus TCP.
    Compatible avec l'architecture des autres modules (VoltronicFleet, JKBMSFleet…).
    """

    def __init__(
        self,
        host: str = "192.168.1.50",
        port: int = 502,
        timeout: float = 5.0,
        poll_interval: int = 10,
        scan_unit_ids: list = None,
        rescan_on_error: bool = True,
    ):
        self.host = host
        self.port = port
        self.poll_interval = poll_interval
        self.scan_unit_ids = scan_unit_ids
        self.rescan_on_error = rescan_on_error
        self._client = VictronModbusClient(host=host, port=port, timeout=timeout)
        self._connected = False
        self._scanned = False

    async def connect(self) -> bool:
        ok = await self._client.connect()
        self._connected = ok
        return ok

    async def disconnect(self):
        await self._client.disconnect()
        self._connected = False
        self._scanned = False

    async def scan(self) -> dict:
        """Force un nouveau scan des devices présents."""
        result = await self._client.scan_devices(self.scan_unit_ids)
        self._scanned = True
        return result

    async def poll_all(self) -> dict:
        """Interroge tout le système. Retourne un dict structuré pour l'API."""
        if not self._connected:
            await self.connect()

        if not self._scanned:
            await self.scan()

        try:
            raw = await self._client.poll_all()
        except Exception as e:
            logger.error("Victron poll_all erreur: %s", e)
            if self.rescan_on_error:
                try:
                    await self.disconnect()
                    await self.connect()
                    await self.scan()
                except Exception:
                    pass
            raise

        # Convertir en dicts unifiés
        result = {
            "system": system_to_dict(raw["system"]) if raw["system"] else None,
            "inverters": {},       # VE.Bus = onduleurs pour l'API
            "solarchargers": {},
            "batteries": {},
        }

        for uid, d in raw["vebus"].items():
            result["inverters"][str(uid)] = vebus_to_dict(d)

        for uid, d in raw["solarchargers"].items():
            result["solarchargers"][str(uid)] = solarcharger_to_dict(d)

        for uid, d in raw["batteries"].items():
            result["batteries"][str(uid)] = battery_to_dict(d)

        return result

    @property
    def discovered_devices(self) -> dict:
        return self._client.discovered
