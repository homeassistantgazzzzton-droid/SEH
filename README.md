# ⚡ Smart Energy Hub

**Plateforme de monitoring énergétique autonome** — Onduleurs, batteries, finance.  
Alternative standalone à Solar Assistant et ICC Software.

---

## 🎯 Fonctionnalités

| Module | Protocoles | Statut |
|--------|-----------|--------|
| **Onduleurs Voltronic/Axpert** | QPIGS/QPIRI/QMOD via TCP ou RS232 | ✅ |
| **Écosystème Victron** | Modbus TCP sur Cerbo GX / Venus OS | ✅ |
| **BMS Pylontech US2000B** | Console RS232 via Elfin EE10 TCP | ✅ |
| **BMS JK-BMS** | Modbus RTU/TCP (PB2A16S, PB1A16S) | ✅ |
| **Finance** | Tarifs fixe / HC-HP / Tempo EDF | ✅ |
| **Dashboard Web** | Temps réel WebSocket + REST API | ✅ |

---

## 🚀 Démarrage rapide

### 1. Cloner et configurer

```bash
git clone https://github.com/votre-repo/smart-energy-hub.git
cd smart-energy-hub
```

### 2. Éditer `docker-compose.yml`

Adapter les variables d'environnement à votre installation :

```yaml
# Exemple : Voltronic TCP + JK-BMS + Finance
INVERTER_TYPE: "voltronic"
INVERTER_HOST: "192.168.1.100"    # IP de votre passerelle TCP
INVERTER_PORT: "8899"

BMS_TYPE: "jkbms"
JKBMS_HOST: "192.168.1.102"       # IP passerelle RS485/TCP
JKBMS_IDS: "1,2"                  # Adresses Modbus des BMS

FINANCE_ENABLED: "true"
FINANCE_IMPORT_PRICE: "0.2516"    # €/kWh EDF
FINANCE_EXPORT_PRICE: "0.1269"    # €/kWh revente
```

### 3. Lancer

```bash
docker compose up -d
```

### 4. Accéder au dashboard

Ouvrir **http://IP_DU_SERVEUR:8080** dans un navigateur.

---

## 📡 Configurations types

### Config A — Voltronic + Pylontech

```yaml
INVERTER_TYPE: "voltronic"
INVERTER_MODE: "tcp"
INVERTER_HOST: "192.168.1.100"
INVERTER_PORT: "8899"

BMS_TYPE: "pylontech"
ELFIN_HOST: "192.168.1.101"
ELFIN_PORT: "9999"
NUM_BATTERIES: "4"
```

### Config B — Voltronic + JK-BMS via USB

```yaml
INVERTER_TYPE: "voltronic"
INVERTER_MODE: "serial"
INVERTER_SERIAL: "/dev/ttyUSB0"

BMS_TYPE: "jkbms"
JKBMS_MODE: "rtu"
JKBMS_SERIAL: "/dev/ttyUSB1"
JKBMS_IDS: "1,2"
```

N'oubliez pas de décommenter la section `devices:` dans le docker-compose.

### Config C — BMS seul (monitoring)

```yaml
INVERTER_TYPE: "none"

BMS_TYPE: "jkbms"
JKBMS_MODE: "tcp"
JKBMS_HOST: "192.168.1.102"
JKBMS_IDS: "1,2,3"
```

### Config D — Écosystème Victron complet

Pour une installation Victron (Cerbo GX + MultiPlus + MPPTs + BMV), le protocole
Modbus TCP du Cerbo expose tous les sous-devices sur un seul IP/port.

**Prérequis sur le Cerbo :**
Settings → Services → Modbus TCP → **ON**

```yaml
INVERTER_TYPE: "victron"
VICTRON_HOST: "192.168.1.50"       # IP du Cerbo GX
VICTRON_PORT: "502"

# Optionnel : limiter le scan à des unit IDs précis pour accélérer le démarrage
# VICTRON_SCAN_IDS: "1,2,3,227,225,100"

# Si la batterie Victron (BMV/Lynx/SmartShunt) fournit déjà le SoC, laisser :
BMS_TYPE: "none"

# Ou ajouter un JK-BMS en parallèle pour surveiller les cellules individuelles :
# BMS_TYPE: "jkbms"
# JKBMS_MODE: "tcp"
# JKBMS_HOST: "192.168.1.102"
```

**Auto-détection :** au démarrage, Smart Energy Hub scanne les unit IDs
Modbus standards (1-46, 100, 220-247) et identifie automatiquement les
devices présents :
- MultiPlus / Quattro → page Onduleur (affichage VE.Bus)
- SmartSolar MPPT → page MPPT (une carte par chargeur)
- BMV / SmartShunt / Lynx Smart BMS → page Batteries
- Cerbo GX (unit 100) → vue système globale

Exemple pour une installation comme 10 kWc + 3× MPPT 250/100 + MultiPlus-II 10000
+ Lynx Smart BMS : tous les devices apparaissent automatiquement en 30 secondes.

---

## 💰 Module Finance

Le moteur financier calcule en temps réel :

- **Coût réel** de l'électricité (import + abonnement - export)
- **Économies** vs consommation 100% réseau
- **Valeur** de la production solaire
- **Rentabilité** de la batterie
- **Autosuffisance** et dépendance réseau
- **Historique** jour / mois / année

### Types de contrat supportés

| Type | Description |
|------|-------------|
| `fixed` | Prix unique import/export |
| `time_based` | HC/HP (jusqu'à 4 plages horaires) |
| `tempo` | Tempo EDF (bleu/blanc/rouge × HC/HP) |

---

## 🔌 API REST

| Endpoint | Description |
|----------|-------------|
| `GET /api/status` | Snapshot complet (onduleur + BMS + MPPTs + finance) |
| `GET /api/inverter` | Données onduleur (Voltronic ou Victron VE.Bus) |
| `GET /api/bms` | Données BMS (JK-BMS, Pylontech, ou Victron battery monitor) |
| `GET /api/solarchargers` | Données MPPT Victron (1 entrée par chargeur) |
| `GET /api/victron_system` | Vue globale Cerbo GX (flux d'énergie) |
| `GET /api/finance` | Données financières |
| `GET /api/config` | Configuration active |
| `WS /ws` | WebSocket temps réel |

---

## 🏗️ Architecture

```
smart-energy-hub/
├── backend/
│   ├── main.py              # FastAPI + WebSocket + polling loops
│   ├── voltronic.py          # Protocole Voltronic/Axpert (QPIGS)
│   ├── victron.py            # Modbus TCP Cerbo GX (MultiPlus+MPPT+BMV)
│   ├── pylontech.py          # Protocole Pylontech RS232
│   ├── jkbms_modbus.py       # Protocole JK-BMS Modbus
│   ├── energy_finance.py     # Moteur calcul financier
│   └── requirements.txt
├── frontend/
│   └── index.html            # Dashboard SPA (vanilla JS)
├── Dockerfile
├── docker-compose.yml
└── README.md
```

### Ajouter un nouveau module

L'architecture est conçue pour être extensible. Pour ajouter un nouvel onduleur ou BMS :

1. Créer `backend/mon_module.py` avec une classe client (méthodes `connect()`, `poll()`, `to_dict()`)
2. Ajouter les variables d'environnement dans `main.py`
3. Créer une boucle de polling async dans `main.py`
4. Ajouter la condition dans le `lifespan`

---

## 📋 Prérequis

- Docker + Docker Compose
- Réseau accessible vers les passerelles TCP (Elfin, Waveshare…)
- Ou accès USB pour les connexions série

---

## 📝 Licence

Ce projet est open source. Contributions bienvenues.
