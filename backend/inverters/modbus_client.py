"""
Smart Energy Hub — Client Modbus TCP unifié pour onduleurs
===========================================================

Wrapper autour de pymodbus async pour :
  - Connexion TCP avec retry
  - Lecture par blocs avec décodage automatique selon RegisterDef
  - Écriture sécurisée (vérifie la whitelist du plugin)
  - Scan de registres (pour calibration manuelle d'un nouveau modèle)
"""

import asyncio
import logging
import struct
from typing import Optional, Any

from pymodbus.client import AsyncModbusTcpClient

from .base import InverterPlugin, RegisterDef, RegisterType, RegisterFunc, WriteSpec

logger = logging.getLogger(__name__)


class ModbusInverterClient:
    """
    Client Modbus TCP pour un onduleur, basé sur un InverterPlugin.

    Usage :
        plugin = SolaxX1HybridGen4(config={"host": "192.168.1.50", "port": 502})
        client = ModbusInverterClient(plugin)
        await client.connect()
        status = await client.read_status()
        await client.disconnect()
    """

    def __init__(self, plugin: InverterPlugin):
        self.plugin = plugin
        self._client: Optional[AsyncModbusTcpClient] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        """Établit la connexion TCP. Retourne True si OK."""
        try:
            self._client = AsyncModbusTcpClient(
                host=self.plugin.host,
                port=self.plugin.port,
                timeout=self.plugin.timeout,
            )
            connected = await self._client.connect()
            if connected:
                logger.info(f"Modbus TCP connecté à {self.plugin.name}")
            else:
                logger.warning(f"Modbus TCP connexion refusée à {self.plugin.name}")
            return connected
        except Exception as e:
            logger.warning(f"Erreur connexion Modbus à {self.plugin.name}: {e}")
            return False

    async def disconnect(self):
        """Ferme la connexion proprement."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.connected

    # ── Lecture ──

    async def read_register(self, reg: RegisterDef) -> Optional[Any]:
        """Lit un seul registre selon sa définition."""
        if not self.is_connected:
            if not await self.connect():
                return None

        async with self._lock:
            try:
                if reg.func == RegisterFunc.HOLDING:
                    rr = await self._client.read_holding_registers(
                        address=reg.address, count=reg.length, slave=self.plugin.unit_id
                    )
                else:
                    rr = await self._client.read_input_registers(
                        address=reg.address, count=reg.length, slave=self.plugin.unit_id
                    )
                if rr.isError():
                    logger.debug(f"Modbus error read {reg.address:#x}: {rr}")
                    return None
                return self._decode_registers(rr.registers, reg)
            except Exception as e:
                logger.debug(f"Read error {reg.address:#x}: {e}")
                return None

    async def read_block(self, start: int, count: int,
                         func: RegisterFunc = RegisterFunc.HOLDING) -> Optional[list]:
        """Lit un bloc continu de registres bruts (16-bit unsigned)."""
        if not self.is_connected:
            if not await self.connect():
                return None

        async with self._lock:
            try:
                if func == RegisterFunc.HOLDING:
                    rr = await self._client.read_holding_registers(
                        address=start, count=count, slave=self.plugin.unit_id
                    )
                else:
                    rr = await self._client.read_input_registers(
                        address=start, count=count, slave=self.plugin.unit_id
                    )
                if rr.isError():
                    logger.debug(f"Modbus block read error {start:#x}+{count}: {rr}")
                    return None
                return list(rr.registers)
            except Exception as e:
                logger.debug(f"Block read error {start:#x}+{count}: {e}")
                return None

    async def read_status(self) -> Optional[dict]:
        """
        Lit tous les registres définis dans le plugin et retourne un dict
        {nom_registre: valeur_décodée}.

        Optimise les lectures en regroupant les registres voisins en blocs.
        """
        blocks = self.plugin.get_register_blocks()
        if not blocks:
            return {}

        result = {}
        for start, count, regs_list, func in blocks:
            raw = await self.read_block(start, count, func)
            if raw is None:
                logger.debug(f"Bloc {start:#x}+{count} a échoué")
                continue
            # Pour chaque registre du bloc, extraire et décoder
            for name, reg in regs_list:
                offset = reg.address - start
                if offset < 0 or offset + reg.length > len(raw):
                    continue
                slice_raw = raw[offset: offset + reg.length]
                value = self._decode_registers(slice_raw, reg)
                if value is not None:
                    result[name] = value

        return result

    async def scan_registers(self, start: int, count: int,
                             func: RegisterFunc = RegisterFunc.HOLDING) -> dict:
        """
        Dump brut d'une plage de registres pour calibration manuelle.

        Utile quand on veut calibrer un nouveau modèle d'onduleur :
        on lit la plage 0x000-0x100 et on regarde les valeurs pour identifier
        quel registre est à quoi.
        """
        # Modbus limit = 125 registres par requête → on découpe
        BATCH = 100
        result = {
            "start": start,
            "count": count,
            "func": func.name,
            "data": {},
            "errors": [],
        }
        for batch_start in range(start, start + count, BATCH):
            batch_count = min(BATCH, start + count - batch_start)
            raw = await self.read_block(batch_start, batch_count, func)
            if raw is None:
                result["errors"].append(f"Bloc {batch_start:#x}+{batch_count}")
                continue
            for i, val in enumerate(raw):
                addr = batch_start + i
                # Stocker en hexa (clé) avec différentes interprétations
                signed = val if val < 32768 else val - 65536
                result["data"][f"0x{addr:04x}"] = {
                    "addr_dec": addr,
                    "u16": val,
                    "s16": signed,
                    "hex": f"0x{val:04x}",
                }
        return result

    # ── Écriture (whitelist obligatoire) ──

    async def write_value(self, key: str, value: float) -> dict:
        """
        Écrit dans un registre, mais SEULEMENT si présent dans la whitelist du plugin.

        Args:
            key: Clé dans WRITE_WHITELIST du plugin
            value: Valeur métier (sera scalée + bornée selon WriteSpec)

        Returns:
            dict avec "success", "error" ou "value_written"
        """
        spec = self.plugin.WRITE_WHITELIST.get(key)
        if not spec:
            return {"success": False,
                    "error": f"Registre '{key}' non autorisé en écriture (pas dans whitelist)"}

        # Bornes
        if value < spec.min_value or value > spec.max_value:
            return {"success": False,
                    "error": f"Valeur {value} hors bornes [{spec.min_value}, {spec.max_value}]"}

        # Scale inverse pour obtenir la valeur brute Modbus
        raw_value = int(round(value / spec.scale))

        # Conversion signed → unsigned 16-bit si nécessaire
        if spec.type == RegisterType.S16 and raw_value < 0:
            raw_value = raw_value + 65536

        if not self.is_connected:
            if not await self.connect():
                return {"success": False, "error": "Connexion impossible"}

        async with self._lock:
            try:
                rr = await self._client.write_register(
                    address=spec.address, value=raw_value, slave=self.plugin.unit_id
                )
                if rr.isError():
                    return {"success": False, "error": f"Modbus error: {rr}"}
                logger.info(f"Wrote {value}{spec.unit} to {self.plugin.name}#{key} "
                            f"(addr={spec.address:#x}, raw={raw_value})")
                return {"success": True, "value_written": value,
                        "raw_value": raw_value, "address": spec.address}
            except Exception as e:
                return {"success": False, "error": str(e)}

    # ── Décodage interne ──

    @staticmethod
    def _decode_registers(registers: list, reg: RegisterDef) -> Any:
        """Décode une liste de registres 16-bit selon le RegisterDef."""
        if not registers:
            return None

        try:
            if reg.type == RegisterType.U16:
                return registers[0] * reg.scale

            elif reg.type == RegisterType.S16:
                val = registers[0]
                if val >= 32768:
                    val -= 65536
                return val * reg.scale

            elif reg.type == RegisterType.U32:
                # High word first (big-endian word)
                val = (registers[0] << 16) | registers[1]
                return val * reg.scale

            elif reg.type == RegisterType.S32:
                val = (registers[0] << 16) | registers[1]
                if val >= 0x80000000:
                    val -= 0x100000000
                return val * reg.scale

            elif reg.type == RegisterType.U32_LSB:
                # Low word first (convention Solax compteurs)
                val = (registers[1] << 16) | registers[0]
                return val * reg.scale

            elif reg.type == RegisterType.S32_LSB:
                val = (registers[1] << 16) | registers[0]
                if val >= 0x80000000:
                    val -= 0x100000000
                return val * reg.scale

            elif reg.type == RegisterType.STRING:
                # Chaque registre = 2 caractères ASCII (big-endian)
                chars = []
                for r in registers:
                    chars.append(chr((r >> 8) & 0xFF))
                    chars.append(chr(r & 0xFF))
                return "".join(chars).strip("\x00 ")

            elif reg.type == RegisterType.BITFIELD:
                return registers[0]  # bits bruts

            else:
                return registers[0]
        except Exception as e:
            logger.debug(f"Decode error: {e}")
            return None
