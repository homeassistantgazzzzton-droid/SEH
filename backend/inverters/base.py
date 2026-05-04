"""
Smart Energy Hub — Interface abstraite pour plugins d'onduleurs Modbus
=======================================================================

Chaque marque hérite de InverterPlugin et déclare :
  - REGISTERS : dict des registres à lire (lecture continue par bloc)
  - WRITE_WHITELIST : registres autorisés en écriture (sécurité)
  - parse_status(raw_registers) : décode les registres en dict de valeurs métier
  - detect_model(serial) : identifie la sous-génération depuis le N° de série
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any


class RegisterType(Enum):
    """Type de décodage du registre Modbus (1 register = 16 bits)."""
    U16 = "u16"          # Unsigned 16-bit
    S16 = "s16"          # Signed 16-bit
    U32 = "u32"          # Unsigned 32-bit (mot haut en premier — convention "big-endian word")
    S32 = "s32"          # Signed 32-bit (mot haut en premier)
    U32_LSB = "u32_lsb"  # Unsigned 32-bit (mot bas en premier — convention Solax/Sungrow)
    S32_LSB = "s32_lsb"  # Signed 32-bit (mot bas en premier — convention Solax)
    STRING = "string"    # ASCII (N registres = 2N caractères)
    BITFIELD = "bitfield"  # Flags binaires


class RegisterFunc(Enum):
    """Fonction Modbus pour la lecture."""
    HOLDING = 3   # Read Holding Registers (FC03)
    INPUT = 4     # Read Input Registers (FC04)


@dataclass
class RegisterDef:
    """
    Définition d'un registre Modbus.

    Attributes:
        address: Adresse Modbus du registre (en décimal ou hexa)
        type: Type de décodage
        scale: Facteur multiplicatif (ex: 0.1 pour W → V*0.1)
        unit: Unité métier ("W", "V", "kWh", "%", "°C"...)
        func: Fonction Modbus (HOLDING par défaut)
        length: Nombre de registres (auto pour U16/S16/U32/S32, requis pour STRING)
        description: Description humaine
        signed: Si True, on interprète les valeurs négatives (alternative à S16/S32)
    """
    address: int
    type: RegisterType
    scale: float = 1.0
    unit: str = ""
    func: RegisterFunc = RegisterFunc.HOLDING
    length: int = 0  # 0 = auto from type
    description: str = ""
    signed: bool = False

    def __post_init__(self):
        if self.length == 0:
            self.length = {
                RegisterType.U16: 1,
                RegisterType.S16: 1,
                RegisterType.U32: 2,
                RegisterType.S32: 2,
                RegisterType.U32_LSB: 2,
                RegisterType.S32_LSB: 2,
                RegisterType.BITFIELD: 1,
                RegisterType.STRING: 7,  # 14 chars par défaut Solax
            }.get(self.type, 1)

    @property
    def end_address(self) -> int:
        """Dernier registre couvert (inclus)."""
        return self.address + self.length - 1


@dataclass
class WriteSpec:
    """
    Spécification d'un registre autorisé en écriture (whitelist).

    Sécurité : pour pouvoir limiter les actions destructrices, chaque écriture
    a son spec avec min/max et type, et le client vérifie ces bornes avant d'envoyer.
    """
    address: int
    type: RegisterType
    scale: float = 1.0
    unit: str = ""
    min_value: float = 0
    max_value: float = 100
    description: str = ""


@dataclass
class InverterStatus:
    """
    État instantané d'un onduleur (résultat de parse_status).

    Tous les champs optionnels — chaque marque remplit ce qu'elle peut.
    """
    # ── Identification ──
    online: bool = False
    serial: Optional[str] = None
    model: Optional[str] = None
    firmware: Optional[str] = None
    inverter_type: Optional[str] = None  # ex: "X1-Hybrid Gen4"

    # ── PV (production solaire) ──
    pv_power: float = 0          # W total
    pv1_power: float = 0         # W string 1
    pv2_power: float = 0         # W string 2
    pv1_voltage: float = 0       # V
    pv2_voltage: float = 0       # V
    pv1_current: float = 0       # A
    pv2_current: float = 0       # A

    # ── AC (réseau) ──
    grid_power: float = 0        # W (positif = import, négatif = export)
    grid_voltage: float = 0      # V
    grid_frequency: float = 0    # Hz
    grid_current: float = 0      # A

    # ── Maison (charge) ──
    load_power: float = 0        # W

    # ── Batterie ──
    battery_power: float = 0     # W (positif = charge, négatif = décharge)
    battery_soc: Optional[float] = None  # %
    battery_voltage: float = 0   # V
    battery_current: float = 0   # A
    battery_temperature: Optional[float] = None  # °C
    battery_capacity: Optional[float] = None  # kWh

    # ── Compteurs énergie (depuis mise en service) ──
    yield_today: float = 0       # kWh produit aujourd'hui
    yield_total: float = 0       # kWh produit cumulé
    import_today: float = 0      # kWh importé aujourd'hui
    import_total: float = 0      # kWh importé cumulé
    export_today: float = 0      # kWh exporté aujourd'hui
    export_total: float = 0      # kWh exporté cumulé
    bat_charge_today: float = 0  # kWh chargé batterie aujourd'hui
    bat_discharge_today: float = 0  # kWh déchargé batterie aujourd'hui

    # ── Status ──
    inverter_status: Optional[str] = None  # "Normal", "Fault", "Standby"...
    inverter_temperature: Optional[float] = None  # °C
    last_update: Optional[float] = None  # timestamp UNIX

    # ── Données brutes (pour debug) ──
    raw: dict = field(default_factory=dict)


class InverterPlugin:
    """
    Classe abstraite pour les plugins d'onduleurs Modbus.

    Chaque marque hérite et override les méthodes ci-dessous.
    """

    # Métadonnées (override par chaque plugin)
    BRAND: str = "unknown"
    MODELS: list = []  # Liste des modèles supportés
    DEFAULT_PORT: int = 502
    DEFAULT_UNIT_ID: int = 1

    # Registres à lire (override avec dict {nom: RegisterDef})
    REGISTERS: dict = {}

    # Registres autorisés en écriture (whitelist)
    WRITE_WHITELIST: dict = {}

    # Registre du serial number (pour identification)
    SERIAL_REGISTER: Optional[RegisterDef] = None

    def __init__(self, config: dict):
        self.config = config
        self.host = config.get("host", "")
        self.port = int(config.get("port", self.DEFAULT_PORT))
        self.unit_id = int(config.get("unit_id", self.DEFAULT_UNIT_ID))
        self.timeout = float(config.get("timeout", 5))
        self._serial: Optional[str] = None
        self._model: Optional[str] = None

    # ── Méthodes à override ──

    def parse_status(self, raw_registers: dict) -> InverterStatus:
        """
        Décode un dict {nom_registre: valeur_brute_décodée} en InverterStatus.

        Args:
            raw_registers: dict {nom_registre: valeur} où valeur a déjà été
                          décodée (signed/unsigned, scaling appliqué) par
                          ModbusClient à partir de REGISTERS.

        Returns:
            InverterStatus avec les champs métier remplis.
        """
        raise NotImplementedError("parse_status() must be implemented by subclass")

    def detect_model(self, serial: str) -> Optional[str]:
        """
        Identifie le modèle/sous-génération depuis le N° de série.

        Args:
            serial: Numéro de série lu sur l'onduleur

        Returns:
            Identifiant du modèle (ex: "x1_hybrid_gen4") ou None si inconnu.
        """
        return None

    # ── Utilitaires ──

    def get_register_blocks(self, max_block_size: int = 100) -> list:
        """
        Calcule les blocs de lecture optimaux pour minimiser les appels Modbus.

        Modbus permet de lire jusqu'à 125 registres consécutifs en un coup.
        On regroupe les registres voisins pour réduire le nombre de requêtes.

        Args:
            max_block_size: Taille max d'un bloc (125 max selon spec Modbus)

        Returns:
            Liste de tuples (start_address, count, [(name, RegisterDef), ...])
        """
        if not self.REGISTERS:
            return []

        # Trier par adresse, séparer holding/input
        by_func = {}
        for name, reg in self.REGISTERS.items():
            by_func.setdefault(reg.func, []).append((name, reg))

        blocks = []
        for func, regs in by_func.items():
            regs.sort(key=lambda nr: nr[1].address)

            current_block = []
            current_start = None
            current_end = None

            for name, reg in regs:
                if current_start is None:
                    current_start = reg.address
                    current_end = reg.end_address
                    current_block = [(name, reg)]
                elif (reg.address - current_end <= 5
                      and reg.end_address - current_start + 1 <= max_block_size):
                    # On peut étendre le bloc (gap < 5 registres = OK)
                    current_end = max(current_end, reg.end_address)
                    current_block.append((name, reg))
                else:
                    # Nouveau bloc
                    blocks.append((current_start,
                                   current_end - current_start + 1,
                                   current_block, func))
                    current_start = reg.address
                    current_end = reg.end_address
                    current_block = [(name, reg)]

            if current_block:
                blocks.append((current_start,
                               current_end - current_start + 1,
                               current_block, func))

        return blocks

    @property
    def name(self) -> str:
        """Nom human-readable de l'instance."""
        return f"{self.BRAND}@{self.host}:{self.port}#{self.unit_id}"
