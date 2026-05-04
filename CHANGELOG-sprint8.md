# Sprint 8 — Solax X1-Hybrid Gen4 (foundation multi-marques)

## ✅ Livré

### Architecture extensible `backend/inverters/`

Nouveau package Python conçu pour accueillir progressivement les autres marques (Growatt, Solis, Sofar, AlphaESS...) :

```
backend/inverters/
  __init__.py            ← Registry des plugins + decorators
  base.py                ← InverterPlugin abstrait, RegisterDef, InverterStatus
  modbus_client.py       ← Client TCP pymodbus avec décodage U16/S16/U32/S32/STRING
  plugin_solax_x1.py     ← Plugin Solax X1-Hybrid Gen3/Gen4
  LICENSE-APACHE-2.0     ← Licence Apache 2.0
  NOTICE                 ← Attribution wills106
```

**Cohabitation** avec Victron/Voltronic : nouveau cache `_cache["solax"]` indépendant. On ne casse rien d'existant.

### Plugin Solax X1-Hybrid Gen4 (basé sur ton serial H4502T...)

Couvre les registres principaux :

**Lecture** (auto-détection génération via le serial) :
- PV1/PV2 : tension, courant, puissance individuelle + total
- AC Grid : tension, courant, puissance signée, fréquence
- Batterie : SoC, tension, courant, puissance, température
- Compteurs : yield today/total, import/export today/total, charge/décharge batterie
- Mode/status : Normal, Wait, Fault, EPS, Standby...
- Firmware DSP/ARM, serial number

**Convention de signe** : Solax FeedIn positif = export, mais SEH utilise positif = import.
Le plugin **inverse automatiquement** le signe pour rester cohérent avec Victron.

**Écriture limitée** (whitelist stricte dans `WRITE_WHITELIST`) :
- `battery_min_capacity` : SoC mini de décharge (10-100%)
- `charger_use_mode` : mode opératoire (Self Use, Force Time, Backup, Feed-in)
- `force_charge_soc` : cible SoC en charge forcée (10-100%)

Toute écriture vérifie les bornes min/max **avant** envoi sur le bus, et logue un audit avec adresse + valeur brute + valeur métier.

### Endpoint `/api/solax/scan` — Calibration manuelle

Pour valider/corriger les mappings sur ton hardware réel :

```
GET /api/solax/scan?inverter_id=solax_main&start=0&count=80&func=HOLDING
```

Retourne pour chaque adresse : valeur brute U16, valeur signée S16, hex.
Tu peux ainsi comparer avec ce que ton onduleur affiche à l'écran (SoC, V grid...) et identifier les bonnes adresses.

### UI Réglages

Nouvelle section **☀️ Solax — Onduleur Modbus TCP** :
- Toggle activation
- Sélecteur Gen3/Gen4
- IP, Port, Modbus Unit ID, Timeout, Poll interval
- Bloc d'aide configuration Waveshare (mode TCP, baud 19200, slave 1)
- Bouton **🔍 Scan registres** qui affiche un tableau interactif des registres lus

### Couche `solax_fleet.py`

Suit le pattern de `victron.py` / `voltronic.py` : poll régulier, cache, gestion connexion/reconnexion. Compatible multi-onduleurs (config est une liste).

### Endpoints API

| Méthode | Endpoint | Description |
|---------|----------|-------------|
| GET | `/api/solax` | État de tous les onduleurs Solax + plugins disponibles |
| GET | `/api/solax/scan?inverter_id=&start=&count=&func=` | Dump brut des registres |
| POST | `/api/solax/write?inverter_id=&key=&value=` | Écriture avec whitelist |

### Tests pytest

**+18 tests** (74 au total) :
- `TestInverterPlugin` (4) : RegisterDef auto-length, blocs de lecture optimisés, registry, plugins inconnus
- `TestModbusDecoding` (5) : décodage U16/S16/U32/S32/STRING avec valeurs limites (négatifs, scaling)
- `TestSolaxPlugin` (5) : détection modèle depuis serial, parse_status avec/sans données, convention de signes, whitelist écriture
- `TestSolaxFleet` (3) : disabled par défaut, enabled, cache initial vide
- `TestSolaxAPI` (1) : endpoint `/api/solax` répond

## ⚠️ Mappings provisoires - À calibrer avec /api/solax/scan

Les registres ci-dessous sont basés sur la doc Solax X1X3-G4 v3.21 et le repo wills106, mais **n'ont pas été testés sur ton hardware réel**. Au premier branchement :

1. Configure ton Waveshare en Modbus TCP (baud 19200, slave 1) si pas déjà fait
2. Active Solax dans Réglages → IP du Waveshare → sauvegarde
3. Va dans Réglages → Solax → bouton **🔍 Scan registres**
4. Compare les valeurs lues avec ce que ton onduleur affiche
5. Si une valeur est aberrante (ex: SoC à 1234%), c'est probablement un mauvais offset ou scaling — colle-moi le résultat du scan, on ajuste

## 📝 Fichiers modifiés / créés

| Fichier | État |
|---------|------|
| `backend/inverters/__init__.py` | NOUVEAU |
| `backend/inverters/base.py` | NOUVEAU |
| `backend/inverters/modbus_client.py` | NOUVEAU |
| `backend/inverters/plugin_solax_x1.py` | NOUVEAU |
| `backend/inverters/LICENSE-APACHE-2.0` | NOUVEAU |
| `backend/inverters/NOTICE` | NOUVEAU |
| `backend/solax_fleet.py` | NOUVEAU |
| `backend/main.py` | +cache solax, init/loop, 3 endpoints, update_config |
| `backend/config_manager.py` | +section solax |
| `backend/tests.py` | +18 tests Sprint 8 |
| `frontend/index.html` | +section Réglages Solax, bouton scan, sauvegarde |
| `Dockerfile` | +COPY solax_fleet.py + dossier inverters/ |

## 🎯 Procédure de mise en route chez toi

1. Déploie le nouveau zip : `docker-compose up -d --build`
2. Vérifie les logs : tu dois voir `Solax fleet activé (0 onduleur(s))` au démarrage
3. Va dans **⚙️ Réglages → ☀️ Solax**
4. Coche "Activer Solax", choisis **X1-Hybrid Gen4 (H4*)**
5. Renseigne l'IP de ton Waveshare RS485-Ethernet, port 502
6. Sauvegarde
7. Clique **🔍 Scan registres** : tableau de valeurs s'affiche
8. Colle-moi le résultat ici, je calibre si besoin

Une fois validé, le polling se fera tout seul toutes les 10s, et tu verras les données dans `/api/solax`. **L'intégration dans la Vue d'ensemble principale viendra au prochain sprint** (sprint 9 : on rend Solax visible dans l'UI principale, on ajoute Growatt/Solis, on active l'écriture sécurisée).

## 🚀 Prochaines étapes (sprints suivants)

- **Sprint 9** : intégration UI principale (flow Solax dans Vue d'ensemble), Growatt + Solis, écriture sécurisée
- **Sprint 10** : Sofar + AlphaESS, mode RTU/USB, page d'aide intégrée
