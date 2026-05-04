#!/usr/bin/env python3
"""
Diagnostic Victron Modbus TCP
Usage : python3 diag_victron.py <IP_CERBO> [PORT]

Teste la connectivité et scanne les unit IDs Victron.
Utile pour diagnostiquer un Cerbo GX qui ne répond pas au scan principal.
"""
import asyncio
import sys
import inspect

from pymodbus.client import AsyncModbusTcpClient


# ── Compatibilité pymodbus 3.7 (slave=) vs 3.8+ (device_id=) ──────────
_sig = inspect.signature(AsyncModbusTcpClient.read_holding_registers)
_params = list(_sig.parameters.keys())
_UNIT_KWARG = "device_id" if "device_id" in _params else "slave"


async def _read(client, address, count, unit):
    kwargs = {"address": address, "count": count, _UNIT_KWARG: unit}
    return await client.read_holding_registers(**kwargs)


async def probe_unit(client, uid: int, verbose: bool = False):
    """Teste un unit ID sur plusieurs adresses connues pour identifier le type."""
    tests = {
        "system (unit 100)":       (800, 1),   # serial Cerbo
        "vebus (MultiPlus)":       (3, 1),     # ac_in L1 voltage
        "solarcharger (MPPT)":     (771, 1),   # battery_voltage
        "battery (BMV/Lynx)":      (259, 1),   # voltage
    }

    results = []
    for label, (addr, count) in tests.items():
        try:
            resp = await _read(client, addr, count, uid)
            if resp.isError():
                if verbose:
                    print(f"    [{label}] ERROR: {resp}")
                continue
            results.append(f"{label} → reg{addr}={resp.registers}")
        except Exception as e:
            if verbose:
                print(f"    [{label}] EXCEPTION: {e} ({type(e).__name__})")
            continue

    return results


async def main(host: str, port: int = 502):
    print(f"╔══════════════════════════════════════════════════════╗")
    print(f"║  Diagnostic Victron Modbus TCP — {host}:{port:<8}   ║")
    print(f"╚══════════════════════════════════════════════════════╝\n")

    # 1. Connexion
    print(f"1. Connexion TCP à {host}:{port}…")
    client = AsyncModbusTcpClient(host=host, port=port, timeout=5)
    ok = await client.connect()
    print(f"   → Client.connect() = {ok}")
    print(f"   → Client.connected = {client.connected}")

    if not ok or not client.connected:
        print("\n❌ Connexion échouée. Vérifier :")
        print("   - Modbus TCP activé sur Cerbo (Settings → Services → Modbus TCP → ON)")
        print("   - Firewall (port 502 accessible)")
        print("   - IP correcte du Cerbo")
        return

    # 2. Warmup
    await asyncio.sleep(0.5)
    print(f"\n2. Test de lecture warmup (unit 100, reg 800 = serial Cerbo, kwarg={_UNIT_KWARG})…")
    try:
        resp = await _read(client, address=800, count=6, unit=100)
        if resp.isError():
            print(f"   ❌ ERROR: {resp}")
        else:
            serial_chars = []
            for w in resp.registers:
                serial_chars.append(chr((w >> 8) & 0xFF))
                serial_chars.append(chr(w & 0xFF))
            serial = "".join(serial_chars).rstrip("\x00").strip()
            print(f"   ✅ Registres: {resp.registers}")
            print(f"   ✅ Serial décodé: '{serial}'")
    except Exception as e:
        print(f"   ❌ Exception: {e} ({type(e).__name__})")

    # 3. Scan complet
    print(f"\n3. Scan des unit IDs…\n")
    unit_ids = (
        list(range(1, 47))      # devices principaux
        + [100]                 # Cerbo system
        + list(range(220, 248)) # BMV / Lynx / Multi secondaires
    )

    found = {}
    for uid in unit_ids:
        results = await probe_unit(client, uid, verbose=False)
        if results:
            found[uid] = results
            print(f"   ✓ Unit {uid}:")
            for r in results:
                print(f"       {r}")

    # 4. Résumé
    print(f"\n╔══════════════════════════════════════════════════════╗")
    print(f"║  Résumé : {len(found)} devices détectés sur {len(unit_ids)} testés           ║")
    print(f"╚══════════════════════════════════════════════════════╝")

    if found:
        print("\n📋 À copier dans votre docker-compose.yml :")
        ids = ",".join(str(u) for u in sorted(found.keys()))
        print(f'      VICTRON_SCAN_IDS: "{ids}"')

    client.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 diag_victron.py <IP_CERBO> [PORT]")
        print("Exemple: python3 diag_victron.py 10.0.4.168")
        sys.exit(1)

    host = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 502

    try:
        asyncio.run(main(host, port))
    except KeyboardInterrupt:
        print("\nInterrompu par l'utilisateur.")
