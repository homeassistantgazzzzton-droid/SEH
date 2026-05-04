"""
Smart Energy Hub — Plugin Solax X1-Hybrid Gen3 / Gen4
======================================================

Mappings de registres Modbus extraits du repo wills106 (Apache 2.0)
puis CALIBRÉS sur hardware réel (Alexis, X1-Hybrid Gen4 5kW, serial H4502T*).

Source: https://github.com/wills106/homeassistant-solax-modbus
Licence: Apache 2.0 - voir LICENSE-APACHE-2.0 et NOTICE
Copyright: Wills (wills106) and contributors

Génération identifiée par préfixe du serial number :
  - Gen3 : H1E*, HU*, XB*, H42*, H43*  (anciennes séries)
  - Gen4 : H450*, H449*, H460*, H475*, H4502T*, H4DE*, H4EE*, PRE*

Architecture des registres Solax X1-Hybrid Gen4 :
  - HOLDING (FC03) plage 0x0000-0x0010 : identification (serial, modèle)
  - HOLDING (FC03) plage 0x001F-0x00FF : config et set points (écriture)
  - INPUT (FC04) plage 0x0000-0x004F : données live
  - INPUT (FC04) plage 0x0050-0x009F : compteurs énergie cumulée

CONVENTION SOLAX U32 :
Les compteurs 32-bit utilisent un ordre **mot bas en premier** (LSB-MSB),
contrairement au standard Modbus "big-endian word". On utilise donc le
type U32_LSB / S32_LSB.

Validation : valeurs vues sur capture écran de l'app Solax (28/04 16:18) :
  - Production jour: 7.2 kWh, Charge jour: 6.1 kWh
  - Vente jour: 2.0 kWh, Achat jour: 0.3 kWh
  - PV1: 967W (312V × 3.1A), PV2: 0W, AC: 36W
  - Grid: 235V, 0.6A, 50.01Hz, FeedIn 59W
  - Battery: SoC=89%, V=295.5V, I=2.9A, P=-862W (décharge), T=35°C
"""

from . import register_plugin
from .base import (
    InverterPlugin, RegisterDef, RegisterType, RegisterFunc,
    WriteSpec, InverterStatus,
)


@register_plugin("solax_x1_hybrid_gen4")
class SolaxX1HybridGen4(InverterPlugin):
    """
    Solax X1-Hybrid Gen4 (préfixes serial : H450, H449, H460, H475, H4D, H4E, PRE)

    Onduleur monophasé hybride avec batterie HV. Modbus TCP via Pocket WiFi
    ou passerelle RS485-Ethernet.
    """

    BRAND = "Solax"
    MODELS = ["X1-Hybrid 3.0kW", "X1-Hybrid 3.7kW", "X1-Hybrid 5.0kW",
              "X1-Hybrid 6.0kW", "X1-Hybrid 7.5kW"]
    DEFAULT_PORT = 502
    DEFAULT_UNIT_ID = 1

    SERIAL_REGISTER = RegisterDef(
        address=0x0000, type=RegisterType.STRING, length=7,
        description="Serial number"
    )

    # ═══════════════════════════════════════════════════════════════════
    # Registres Solax X1-Hybrid Gen4
    # Validés contre repo wills106/homeassistant-solax-modbus
    # ═══════════════════════════════════════════════════════════════════
    REGISTERS = {
        # ── Identification (HOLDING 0x0000-0x0006) ──
        "serial": RegisterDef(
            0x0000, RegisterType.STRING, length=7,
            func=RegisterFunc.HOLDING,
            description="Serial number"),

        # ── Données live (INPUT registers FC04) ──
        # Grid (réseau côté inverter, pas CT clamp)
        "inverter_voltage": RegisterDef(
            0x0000, RegisterType.U16, scale=0.1, unit="V",
            func=RegisterFunc.INPUT,
            description="Inverter AC voltage"),
        "inverter_current": RegisterDef(
            0x0001, RegisterType.S16, scale=0.1, unit="A",
            func=RegisterFunc.INPUT,
            description="Inverter AC current (signed)"),
        "inverter_power": RegisterDef(
            0x0002, RegisterType.S16, unit="W",
            func=RegisterFunc.INPUT,
            description="Inverter output power (signed)"),
        "grid_frequency": RegisterDef(
            0x0007, RegisterType.U16, scale=0.01, unit="Hz",
            func=RegisterFunc.INPUT,
            description="Grid frequency"),
        "inverter_temperature": RegisterDef(
            0x0008, RegisterType.S16, unit="°C",
            func=RegisterFunc.INPUT,
            description="Inverter internal temperature"),
        "run_mode": RegisterDef(
            0x0009, RegisterType.U16,
            func=RegisterFunc.INPUT,
            description="Run mode"),

        # PV strings (INPUT) — adresses validées contre repo wills106 + Gen4 docs
        # IMPORTANT : sur Gen4 X1, les PV power sont en W direct (pas calculés depuis V×A)
        "pv1_voltage": RegisterDef(
            0x0003, RegisterType.U16, scale=0.1, unit="V",
            func=RegisterFunc.INPUT, description="PV1 voltage (×0.1)"),
        "pv2_voltage": RegisterDef(
            0x0004, RegisterType.U16, scale=0.1, unit="V",
            func=RegisterFunc.INPUT, description="PV2 voltage (×0.1)"),
        "pv1_current": RegisterDef(
            0x0005, RegisterType.U16, scale=0.1, unit="A",
            func=RegisterFunc.INPUT, description="PV1 current (×0.1)"),
        "pv2_current": RegisterDef(
            0x0006, RegisterType.U16, scale=0.1, unit="A",
            func=RegisterFunc.INPUT, description="PV2 current (×0.1)"),
        "pv1_power": RegisterDef(
            0x000A, RegisterType.U16, unit="W",
            func=RegisterFunc.INPUT, description="PV1 power (W direct)"),
        "pv2_power": RegisterDef(
            0x000B, RegisterType.U16, unit="W",
            func=RegisterFunc.INPUT, description="PV2 power (W direct)"),

        # Battery (INPUT) - vérifié : SoC=89%, V=295.5V, I=2.9A, P=-862W (décharge)
        "battery_voltage": RegisterDef(
            0x0014, RegisterType.U16, scale=0.1, unit="V",
            func=RegisterFunc.INPUT,
            description="Battery voltage (HV ~250-450V)"),
        "battery_current": RegisterDef(
            0x0015, RegisterType.S16, scale=0.1, unit="A",
            func=RegisterFunc.INPUT,
            description="Battery current (+ = charge)"),
        "battery_power": RegisterDef(
            0x0016, RegisterType.S16, unit="W",
            func=RegisterFunc.INPUT,
            description="Battery power (+ = charge, - = décharge)"),
        "battery_temperature": RegisterDef(
            0x0018, RegisterType.S16, unit="°C",
            func=RegisterFunc.INPUT,
            description="Battery temperature"),
        "battery_soc": RegisterDef(
            0x001C, RegisterType.U16, unit="%",
            func=RegisterFunc.INPUT,
            description="Battery State of Charge"),

        # Grid via CT clamp (mesure réelle au point de soutirage)
        # Convention Solax pour `measured_power` (0x46) — confirmée ligne 299 du repo wills106 :
        #   positif = import (consomme du réseau)
        #   négatif = export (injecte sur le réseau)
        # Format : REGISTER_S32 LSB-MSB (mot bas en premier).
        # Note : la doc wills106 indique REGISTER_S32 standard, mais sur le firmware
        # Alexis (X1-Hybrid Gen4 H4502TI3474072), validé empiriquement que c'est LSB-MSB
        # (les compteurs 0x48/0x4A/0x52 sont aussi en LSB-MSB sur ce firmware).
        "feedin_power": RegisterDef(
            0x0046, RegisterType.S32_LSB, unit="W",
            func=RegisterFunc.INPUT,
            description="Grid power CT clamp (S32 LSB-MSB) - + import / - export"),

        # ── Compteurs énergie cumulée (INPUT) ──
        # IMPORTANT : tous les U32 sont en LSB-MSB (mot bas en premier)
        "yield_today": RegisterDef(
            0x0050, RegisterType.U16, scale=0.1, unit="kWh",
            func=RegisterFunc.INPUT,
            description="Today's yield (PV produit)"),
        "yield_total": RegisterDef(
            0x0052, RegisterType.U32_LSB, scale=0.1, unit="kWh",
            func=RegisterFunc.INPUT,
            description="Total yield (PV cumulé)"),
        "feedin_energy_total": RegisterDef(
            0x0048, RegisterType.S32_LSB, scale=0.01, unit="kWh",
            func=RegisterFunc.INPUT,
            description="Energy exported total to grid"),
        "consume_energy_total": RegisterDef(
            0x004A, RegisterType.U32_LSB, scale=0.01, unit="kWh",
            func=RegisterFunc.INPUT,
            description="Energy imported total from grid"),
        "feedin_energy_today": RegisterDef(
            0x0098, RegisterType.U32_LSB, scale=0.01, unit="kWh",
            func=RegisterFunc.INPUT,
            description="Energy exported today to grid"),
        "consume_energy_today": RegisterDef(
            0x009A, RegisterType.U32_LSB, scale=0.01, unit="kWh",
            func=RegisterFunc.INPUT,
            description="Energy imported today from grid"),
    }

    # Whitelist écriture (HOLDING registers - sécurité stricte)
    WRITE_WHITELIST = {
        # Adresses depuis repo wills106 plugin_solax.py
        "battery_min_capacity": WriteSpec(
            address=0x0032, type=RegisterType.U16, scale=1, unit="%",
            min_value=10, max_value=100,
            description="Minimum battery SoC (battery won't discharge below this)"
        ),
        "charger_use_mode": WriteSpec(
            address=0x001F, type=RegisterType.U16, scale=1, unit="",
            min_value=0, max_value=3,
            description="0=Self Use, 1=Force Time, 2=Back Up, 3=Feed-in Priority"
        ),
        "force_charge_soc": WriteSpec(
            address=0x0029, type=RegisterType.U16, scale=1, unit="%",
            min_value=10, max_value=100,
            description="Force charge target SoC"
        ),
    }

    # Codes "run_mode" Solax Gen4
    RUN_MODES = {
        0: "Wait", 1: "Check", 2: "Normal", 3: "Fault",
        4: "Permanent Fault", 5: "Update", 6: "EPS Check",
        7: "EPS", 8: "Self-Test", 9: "Idle", 10: "Standby",
    }

    def detect_model(self, serial: str) -> str:
        """Identifie la sous-génération depuis le serial.

        D'après le repo wills106, les préfixes Gen4 X1 sont :
          H43x, H449, H450, H460, H475, H4502T*, PRE
        """
        if not serial or len(serial) < 4:
            return "x1_hybrid_gen4_unknown"
        s = serial.upper()
        if s.startswith("H4502") or s.startswith("H450"):
            return "x1_hybrid_gen4_5kw"
        elif s.startswith("H449"):
            return "x1_hybrid_gen4_5kw"
        elif s.startswith("H43"):
            return "x1_hybrid_gen4_3kw"
        elif s.startswith("H460"):
            return "x1_hybrid_gen4_6kw"
        elif s.startswith("H475"):
            return "x1_hybrid_gen4_7_5kw"
        elif s.startswith("PRE"):
            return "x1_hybrid_gen4_retrofit"
        elif s.startswith("H4D") or s.startswith("H4E"):
            return "x1_hybrid_gen4"
        else:
            return "x1_hybrid_gen4_generic"

    def parse_status(self, raw: dict) -> InverterStatus:
        """Décode les registres bruts en InverterStatus."""
        status = InverterStatus(
            online=True,
            serial=raw.get("serial"),
            inverter_type="X1-Hybrid Gen4",
            raw=raw,
        )

        if status.serial:
            status.model = self.detect_model(status.serial)

        # PV
        status.pv1_voltage = raw.get("pv1_voltage", 0) or 0
        status.pv2_voltage = raw.get("pv2_voltage", 0) or 0
        status.pv1_current = raw.get("pv1_current", 0) or 0
        status.pv2_current = raw.get("pv2_current", 0) or 0
        status.pv1_power = raw.get("pv1_power", 0) or 0
        status.pv2_power = raw.get("pv2_power", 0) or 0
        status.pv_power = status.pv1_power + status.pv2_power

        # AC Grid (mesure CT clamp = vraie puissance au compteur)
        status.grid_voltage = raw.get("inverter_voltage", 0) or 0
        status.grid_current = raw.get("inverter_current", 0) or 0
        status.grid_frequency = raw.get("grid_frequency", 0) or 0

        # Grid power : convention SEH (+ import, - export).
        # Sur Solax `measured_power` (0x46) suit déjà cette convention.
        status.grid_power = raw.get("feedin_power", 0) or 0

        # Batterie
        # IMPORTANT : sur Solax Gen2/3/4/5, le registre 0x16 (battery_power_charge)
        # utilise une convention NATIVE inversée : positif = décharge, négatif = charge.
        # HA applique invert=True (cf wills106 plugin_solax.py ligne 10394).
        # On fait pareil pour rester cohérent avec la convention SEH (positif = charge).
        status.battery_voltage = raw.get("battery_voltage", 0) or 0
        bat_current_raw = raw.get("battery_current", 0) or 0
        status.battery_current = -bat_current_raw  # invert
        bat_power_raw = raw.get("battery_power", 0) or 0
        status.battery_power = -bat_power_raw  # invert : SEH +charge, -décharge
        status.battery_temperature = raw.get("battery_temperature")
        status.battery_soc = raw.get("battery_soc")

        # Estimation puissance maison :
        #   maison = inverter_power + grid_power
        # (ligne 1059 wills106 : house_load = inv - meas_power, adapté à notre convention
        #  où grid_power = +meas si import, -meas si export, donc maison = inv + grid_power)
        inv_power = raw.get("inverter_power", 0) or 0
        status.load_power = max(0, inv_power + status.grid_power)

        # Compteurs énergie
        status.yield_today = raw.get("yield_today", 0) or 0
        status.yield_total = raw.get("yield_total", 0) or 0
        status.export_today = raw.get("feedin_energy_today", 0) or 0
        status.export_total = raw.get("feedin_energy_total", 0) or 0
        status.import_today = raw.get("consume_energy_today", 0) or 0
        status.import_total = raw.get("consume_energy_total", 0) or 0
        # Pas de today direct pour batterie sur Gen4 → calculé en aval
        status.bat_charge_today = 0
        status.bat_discharge_today = 0

        # Status
        run_mode = raw.get("run_mode")
        if run_mode is not None:
            mode_int = int(run_mode)
            status.inverter_status = self.RUN_MODES.get(mode_int, f"Unknown ({mode_int})")

        status.inverter_temperature = raw.get("inverter_temperature")

        return status


@register_plugin("solax_x1_hybrid_gen3")
class SolaxX1HybridGen3(SolaxX1HybridGen4):
    """Solax X1-Hybrid Gen3 (sérials H1E*, HU*, XB*, H42*, H43*).

    Hérite de Gen4. Les registres principaux sont identiques sur Gen3/Gen4
    pour cette gamme (l'évolution porte surtout sur les set points et le BMS).
    À différencier au prochain sprint si nécessaire d'après les retours.
    """

    MODELS = ["X1-Hybrid 3.0kW Gen3", "X1-Hybrid 3.7kW Gen3",
              "X1-Hybrid 4.6kW Gen3", "X1-Hybrid 5.0kW Gen3"]

    def detect_model(self, serial: str) -> str:
        if not serial or len(serial) < 4:
            return "x1_hybrid_gen3_unknown"
        return "x1_hybrid_gen3_generic"

    def parse_status(self, raw: dict) -> InverterStatus:
        status = super().parse_status(raw)
        status.inverter_type = "X1-Hybrid Gen3"
        return status
