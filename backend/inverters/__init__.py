"""
Smart Energy Hub — Inverters package
====================================

Architecture multi-marques pour onduleurs Modbus (Solax, Growatt, Solis, etc.)

Structure :
  base.py              ← Classe abstraite InverterPlugin
  modbus_client.py     ← Client TCP pymodbus unifié
  plugin_solax_x1.py   ← Plugin Solax X1-Hybrid Gen3/Gen4

Pour ajouter une nouvelle marque :
  1. Créer plugin_<marque>.py qui hérite de InverterPlugin
  2. Définir REGISTERS = {nom: RegisterDef(addr, type, scale, ...)}
  3. Implémenter parse_status() et detect_model()
  4. Enregistrer dans PLUGIN_REGISTRY ci-dessous
"""

from .base import InverterPlugin, RegisterDef, RegisterType

# Registry des plugins disponibles
# (rempli au runtime par les plugins eux-mêmes via decorator @register_plugin)
PLUGIN_REGISTRY: dict = {}


def register_plugin(name: str):
    """Decorator utilisé par les plugins pour s'enregistrer."""
    def decorator(cls):
        PLUGIN_REGISTRY[name] = cls
        return cls
    return decorator


def get_plugin(name: str):
    """Récupère un plugin par son nom."""
    return PLUGIN_REGISTRY.get(name)


def list_plugins() -> list:
    """Liste tous les plugins enregistrés."""
    return list(PLUGIN_REGISTRY.keys())


# Forcer l'import des plugins pour qu'ils s'enregistrent
from . import plugin_solax_x1  # noqa: F401, E402

__all__ = ["InverterPlugin", "RegisterDef", "RegisterType",
           "register_plugin", "get_plugin", "list_plugins",
           "PLUGIN_REGISTRY"]
