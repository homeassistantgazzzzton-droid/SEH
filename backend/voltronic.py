"""
Voltronic / Axpert / MPP Solar Inverter Client
Protocole : commandes texte avec CRC16 CCITT (XMODEM)
Commandes principales :
  QPIGS  → état temps réel (tension, courant, puissance, SoC batterie…)
  QPIRI  → paramètres de configuration
  QMOD   → mode de fonctionnement

Connexion possible via :
  - Passerelle TCP/IP (Elfin EE10, Waveshare…)  → mode TCP
  - Adaptateur USB→RS232 (/dev/ttyUSB0, /dev/hidraw0) → mode Serial
"""
import asyncio
import logging
import struct
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── CRC16 XMODEM ────────────────────────────────────────────────────────────
_CRC_TABLE = []
for _i in range(256):
    _crc = _i << 8
    for _ in range(8):
        _crc = (_crc << 1) ^ 0x1021 if _crc & 0x8000 else _crc << 1
    _CRC_TABLE.append(_crc & 0xFFFF)


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((_CRC_TABLE[((crc >> 8) ^ b) & 0xFF]) ^ (crc << 8)) & 0xFFFF
    return crc


def make_command(cmd: str) -> bytes:
    """Encode une commande Voltronic avec CRC16 + CR."""
    data = cmd.encode("ascii")
    crc = crc16_xmodem(data)
    hi = (crc >> 8) & 0xFF
    lo = crc & 0xFF
    # Certains onduleurs refusent les octets 0x28 '(' et 0x0D '\r' dans le CRC
    # On les envoie tels quels, la plupart des firmwares les acceptent
    return data + bytes([hi, lo]) + b"\r"


def validate_response(raw: bytes) -> str:
    """Valide et extrait le payload d'une réponse Voltronic."""
    if not raw or raw[0:1] != b"(":
        raise ValueError(f"Réponse invalide (pas de parenthèse): {raw[:40]}")
    # Trouver le CR final
    cr_pos = raw.rfind(b"\r")
    if cr_pos < 3:
        # Pas de CR, essayer sans
        cr_pos = len(raw)
    # Les 2 octets avant le CR sont le CRC
    payload = raw[1:cr_pos - 2]
    return payload.decode("ascii", errors="replace").strip()


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class VoltronicStatus:
    """Données temps réel d'un onduleur Voltronic (QPIGS)."""
    inverter_id: int = 1

    # Réseau / Grid
    grid_voltage: Optional[float] = None       # V
    grid_frequency: Optional[float] = None     # Hz

    # Sortie / Output
    output_voltage: Optional[float] = None     # V
    output_frequency: Optional[float] = None   # Hz
    output_apparent_power: Optional[int] = None  # VA
    output_active_power: Optional[int] = None    # W
    output_load_percent: Optional[int] = None    # %

    # Bus DC
    bus_voltage: Optional[float] = None        # V

    # Batterie
    battery_voltage: Optional[float] = None    # V
    battery_charge_current: Optional[int] = None  # A
    battery_capacity: Optional[int] = None     # % (SoC)
    battery_discharge_current: Optional[int] = None  # A
    inverter_heat_sink_temp: Optional[int] = None    # °C
    pv_input_current: Optional[float] = None   # A (courant PV total)
    pv_input_voltage: Optional[float] = None   # V
    battery_voltage_scc: Optional[float] = None  # V (tension bat vue par SCC)
    pv_input_power: Optional[int] = None       # W

    # Status bits
    device_status: str = ""
    # Décodage des bits de statut
    charging_scc: bool = False        # SCC charge active
    charging_ac: bool = False         # AC charge active
    charging_scc_and_ac: bool = False
    load_on: bool = False
    battery_voltage_steady: bool = False
    sbu_priority: bool = False        # Solar/Battery/Utility priority

    # Mode de fonctionnement (QMOD)
    mode: str = ""  # P=Power On, S=Standby, L=Line, B=Battery, F=Fault

    # Paramètres (QPIRI)
    max_charge_current: Optional[int] = None
    max_grid_charge_current: Optional[int] = None
    rated_power_va: Optional[int] = None
    battery_type: str = ""
    output_source_priority: str = ""
    charger_source_priority: str = ""

    online: bool = False


def parse_qpigs(response: str, inverter_id: int = 1) -> VoltronicStatus:
    """
    Parse la réponse QPIGS.
    Format typique (espace-séparé) :
    BBB.B CC.C DDD.D EE.E FFFF GGGG HHH II.I JJ.J KKK LLL MMMM NNNN OO.O PPP.P QQQQQ bbbbbbbbb
    """
    d = VoltronicStatus(inverter_id=inverter_id)
    parts = response.split()

    if len(parts) < 16:
        logger.warning("QPIGS: réponse trop courte (%d champs)", len(parts))
        d.online = False
        return d

    try:
        d.grid_voltage = float(parts[0])
        d.grid_frequency = float(parts[1])
        d.output_voltage = float(parts[2])
        d.output_frequency = float(parts[3])
        d.output_apparent_power = int(parts[4])
        d.output_active_power = int(parts[5])
        d.output_load_percent = int(parts[6])
        d.bus_voltage = float(parts[7])
        d.battery_voltage = float(parts[8])
        d.battery_charge_current = int(parts[9])
        d.battery_capacity = int(parts[10])
        d.inverter_heat_sink_temp = int(parts[11])
        d.pv_input_current = float(parts[12])
        d.pv_input_voltage = float(parts[13])
        d.battery_voltage_scc = float(parts[14])
        d.battery_discharge_current = int(parts[15])

        # Status bits (champ 16 ou 17 selon firmware)
        status_field = None
        pv_power_field = None

        if len(parts) >= 18:
            # Format avec PV power en champ 16 et status en champ 17
            try:
                pv_power_field = int(parts[16])
                status_field = parts[17]
            except ValueError:
                status_field = parts[16]
        elif len(parts) >= 17:
            status_field = parts[16]

        if pv_power_field is not None:
            d.pv_input_power = pv_power_field
        elif d.pv_input_current and d.pv_input_voltage:
            d.pv_input_power = int(d.pv_input_current * d.pv_input_voltage)

        if status_field and len(status_field) >= 8:
            d.device_status = status_field
            bits = status_field
            d.sbu_priority = bits[0] == "1"
            # bit 1 = configuration changed
            d.charging_scc = bits[2] == "1"
            d.charging_ac = bits[3] == "1"
            d.charging_scc_and_ac = bits[4] == "1"
            d.load_on = bits[5] == "1"
            d.battery_voltage_steady = bits[6] == "1"
            # bit 7 = charge on

        d.online = True

    except (ValueError, IndexError) as e:
        logger.error("QPIGS parse error: %s — raw: %s", e, response[:100])
        d.online = False

    return d


def parse_qpiri(response: str, status: VoltronicStatus):
    """Parse la réponse QPIRI (paramètres de configuration)."""
    parts = response.split()
    if len(parts) < 15:
        return
    try:
        status.rated_power_va = int(float(parts[1]))
        status.max_charge_current = int(float(parts[5]))
        status.max_grid_charge_current = int(float(parts[12]))

        # Battery type (champ 9)
        bat_types = {"0": "AGM", "1": "Flooded", "2": "User", "3": "Pylontech",
                     "4": "Shinheung", "5": "Weco", "6": "Soltaro", "8": "LIB",
                     "9": "Ternary"}
        if len(parts) > 9:
            status.battery_type = bat_types.get(parts[9], parts[9])

        # Output source priority (champ 10)
        out_prio = {"0": "Utility", "1": "Solar", "2": "SBU"}
        if len(parts) > 10:
            status.output_source_priority = out_prio.get(parts[10], parts[10])

        # Charger priority (champ 11)
        chg_prio = {"0": "Utility first", "1": "Solar first",
                    "2": "Solar+Utility", "3": "Solar only"}
        if len(parts) > 11:
            status.charger_source_priority = chg_prio.get(parts[11], parts[11])

    except (ValueError, IndexError) as e:
        logger.debug("QPIRI parse: %s", e)


def parse_qmod(response: str) -> str:
    """Parse la réponse QMOD → mode char."""
    modes = {
        "P": "PowerOn", "S": "Standby", "L": "Line",
        "B": "Battery", "F": "Fault", "H": "Power Saving",
        "D": "Shutdown", "Y": "Bypass", "E": "ECO",
    }
    if response:
        mode_char = response.strip()[0] if response.strip() else ""
        return modes.get(mode_char, mode_char)
    return ""


class VoltronicClient:
    """
    Client asynchrone pour onduleurs Voltronic/Axpert.
    Supporte TCP (passerelle) et Serial (USB/RS232).
    """

    def __init__(
        self,
        mode: str = "tcp",
        host: str = "192.168.1.100",
        port: int = 8899,
        serial_port: str = "/dev/ttyUSB0",
        baudrate: int = 2400,
        inverter_id: int = 1,
        timeout: float = 5.0,
    ):
        self.mode = mode.lower()
        self.host = host
        self.port = port
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.inverter_id = inverter_id
        self.timeout = timeout
        self._reader = None
        self._writer = None
        self._serial = None
        self._lock = asyncio.Lock()
        self._info_read = False

    async def connect(self) -> bool:
        if self.mode == "tcp":
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port),
                    timeout=self.timeout
                )
                logger.info("Voltronic TCP connecté %s:%d (inv #%d)",
                            self.host, self.port, self.inverter_id)
                return True
            except Exception as e:
                logger.error("Voltronic TCP connexion échouée: %s", e)
                return False
        else:
            try:
                import serial_asyncio
                self._reader, self._writer = await serial_asyncio.open_serial_connection(
                    url=self.serial_port,
                    baudrate=self.baudrate,
                    bytesize=8,
                    parity="N",
                    stopbits=1,
                )
                logger.info("Voltronic Serial connecté %s @%d baud (inv #%d)",
                            self.serial_port, self.baudrate, self.inverter_id)
                return True
            except ImportError:
                logger.error("Module serial_asyncio non disponible. "
                             "Installer: pip install pyserial-asyncio")
                return False
            except FileNotFoundError:
                logger.error("Port série %s introuvable. "
                             "Vérifier que le device est mappé dans docker-compose "
                             "(devices: - /dev/ttyUSB0:/dev/ttyUSB0) et que "
                             "group_add: [dialout] est configuré.", self.serial_port)
                return False
            except PermissionError:
                logger.error("Permission refusée sur %s. "
                             "Ajouter group_add: [dialout] dans docker-compose.",
                             self.serial_port)
                return False
            except Exception as e:
                logger.error("Voltronic Serial connexion échouée %s: %s (%s)",
                             self.serial_port, e, type(e).__name__)
                return False

    async def disconnect(self):
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    async def _send_command(self, cmd: str) -> str:
        """Envoie une commande et retourne la réponse parsée."""
        async with self._lock:
            if not self._writer:
                raise ConnectionError("Non connecté")

            raw_cmd = make_command(cmd)

            # Vider le buffer
            try:
                if self._reader:
                    await asyncio.wait_for(self._reader.read(4096), timeout=0.2)
            except asyncio.TimeoutError:
                pass

            self._writer.write(raw_cmd)
            await self._writer.drain()

            # Lire la réponse
            chunks = []
            deadline = asyncio.get_event_loop().time() + self.timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    chunk = await asyncio.wait_for(
                        self._reader.read(4096),
                        timeout=min(remaining, 1.0)
                    )
                    if not chunk:
                        break
                    chunks.append(chunk)
                    raw = b"".join(chunks)
                    if b"\r" in raw:
                        break
                except asyncio.TimeoutError:
                    if chunks:
                        break
                    continue

            raw = b"".join(chunks)
            if not raw:
                raise TimeoutError(f"Pas de réponse pour {cmd}")

            return validate_response(raw)

    async def poll(self) -> VoltronicStatus:
        """Interroge l'onduleur (QPIGS + QPIRI + QMOD)."""
        # QPIGS — données temps réel
        resp = await self._send_command("QPIGS")
        status = parse_qpigs(resp, self.inverter_id)

        # QMOD — mode de fonctionnement
        try:
            resp_mod = await self._send_command("QMOD")
            status.mode = parse_qmod(resp_mod)
        except Exception as e:
            logger.debug("QMOD inv #%d: %s", self.inverter_id, e)

        # QPIRI — paramètres (une seule fois)
        if not self._info_read:
            try:
                resp_piri = await self._send_command("QPIRI")
                parse_qpiri(resp_piri, status)
                self._info_read = True
            except Exception as e:
                logger.debug("QPIRI inv #%d: %s", self.inverter_id, e)

        return status

    def to_dict(self, d: VoltronicStatus) -> dict:
        """Convertit en dict unifié pour l'API."""
        # Calcul puissance PV
        pv_power = d.pv_input_power or 0
        # Puissance batterie (+ = charge, - = décharge)
        bat_power = 0
        if d.battery_voltage:
            if d.battery_charge_current:
                bat_power = round(d.battery_voltage * d.battery_charge_current, 1)
            elif d.battery_discharge_current:
                bat_power = round(-d.battery_voltage * d.battery_discharge_current, 1)

        return {
            "id": d.inverter_id,
            "type": "voltronic",
            # Grid
            "grid_voltage": d.grid_voltage,
            "grid_frequency": d.grid_frequency,
            # Output
            "output_voltage": d.output_voltage,
            "output_frequency": d.output_frequency,
            "output_apparent_power": d.output_apparent_power,
            "output_active_power": d.output_active_power,
            "output_load_percent": d.output_load_percent,
            # PV
            "pv_input_voltage": d.pv_input_voltage,
            "pv_input_current": d.pv_input_current,
            "pv_input_power": pv_power,
            # Battery
            "battery_voltage": d.battery_voltage,
            "battery_charge_current": d.battery_charge_current,
            "battery_discharge_current": d.battery_discharge_current,
            "battery_capacity": d.battery_capacity,
            "battery_power": bat_power,
            "battery_voltage_scc": d.battery_voltage_scc,
            # Temperatures
            "inverter_temperature": d.inverter_heat_sink_temp,
            # Status
            "mode": d.mode,
            "device_status": d.device_status,
            "charging_scc": d.charging_scc,
            "charging_ac": d.charging_ac,
            "load_on": d.load_on,
            "sbu_priority": d.sbu_priority,
            # Config
            "rated_power_va": d.rated_power_va,
            "battery_type": d.battery_type,
            "output_source_priority": d.output_source_priority,
            "charger_source_priority": d.charger_source_priority,
            "max_charge_current": d.max_charge_current,
            "max_grid_charge_current": d.max_grid_charge_current,
            # Computed
            "bus_voltage": d.bus_voltage,
            "online": d.online,
        }


class VoltronicFleet:
    """
    Gère un ou plusieurs onduleurs Voltronic en parallèle.
    Pour les systèmes multi-onduleurs (parallel IDs).
    """

    def __init__(
        self,
        mode: str = "tcp",
        host: str = "192.168.1.100",
        port: int = 8899,
        serial_port: str = "/dev/ttyUSB0",
        baudrate: int = 2400,
        inverter_ids: list = None,
        timeout: float = 5.0,
        poll_interval: int = 10,
    ):
        self.poll_interval = poll_interval
        self._clients: dict[int, VoltronicClient] = {}

        for inv_id in (inverter_ids or [1]):
            self._clients[inv_id] = VoltronicClient(
                mode=mode, host=host, port=port,
                serial_port=serial_port, baudrate=baudrate,
                inverter_id=inv_id, timeout=timeout,
            )

    async def poll_all(self) -> list:
        """Interroge tous les onduleurs."""
        results = []
        for inv_id, client in self._clients.items():
            try:
                if not client._writer:
                    await client.connect()
                status = await client.poll()
                results.append(status)
                logger.info("Inverter %d: %.0fW load, PV=%.0fW, Bat=%d%% %s",
                            inv_id,
                            status.output_active_power or 0,
                            status.pv_input_power or 0,
                            status.battery_capacity or 0,
                            status.mode)
            except Exception as e:
                logger.error("Erreur inverter %d: %s", inv_id, e)
                try:
                    await client.disconnect()
                except Exception:
                    pass
                dead = VoltronicStatus(inverter_id=inv_id, online=False)
                results.append(dead)
        return results
