# Sprint 6 — Intelligence et intégration

## ✅ Livré

### 1. 🔮 Prévision vs réel (nouvel onglet Prévisions)

**Persistence automatique** : à chaque consultation de `/api/forecast`, les prévisions journalières sont archivées dans la nouvelle table `solar_forecast_snapshots` de la DB. Idempotent : une seule prévision stockée par paire (snapshot_date, forecast_date), on écrase la plus récente.

**Comparaison différée** : après 1-2 jours, les prévisions sont comparées avec la production réelle mesurée dans `samples_daily`. Le système privilégie la prévision faite la veille (horizon=1), puis celle du matin même si pas d'autre dispo.

**Metrics calculés** :
- **MAE** (Mean Absolute Error, kWh) : erreur absolue moyenne
- **MAPE** (Mean Absolute Percentage Error, %) : erreur relative moyenne
- **Bias** (kWh signé) : si positif, modèle pessimiste ; si négatif, optimiste
- **R²** : coefficient de détermination (0 à 1, plus proche de 1 = meilleur)
- **Qualité** : classification auto (excellent < 15% MAPE / good < 25% / fair < 40% / poor)

**UI** : 4 KPIs en tête (qualité, MAE, MAPE, biais), bilan période prévu vs réel cumulé, graphique Chart.js superposant prévu (ligne jaune pointillée) et réel (zone verte).

**Endpoint** : `GET /api/forecast/accuracy?days=60`

### 2. 🚗 Optimiseur charge Nissan Leaf (nouvel onglet Leaf)

**Algorithme** :
1. Récupère la prévision horaire PV pour le jour cible (aujourd'hui/demain/après-demain)
2. Soustrait la consommation moyenne maison par heure (calculée sur 14 derniers jours)
3. Pour chaque heure dans la fenêtre préférée (ex: 10h-17h), calcule le surplus disponible
4. Trie les heures par surplus décroissant, sélectionne jusqu'à atteindre le besoin
5. Si surplus < besoin et `allow_grid_import` activé, complète depuis grid
6. Retourne : créneau recommandé, kWh depuis solaire/grid, SoC estimé après charge

**UI** :
- Hero card : SoC actuel/cible avec barre de progression, repère cible
- Tabs : Aujourd'hui / Demain / Après-demain
- Plan de charge : 4 KPIs (à charger, solaire dispo, depuis PV, depuis grid)
- Badge statut : ✅ optimal / ⚠️ complément grid / ⚠️ surplus insuffisant
- Détail heure par heure : barres de puissance utilisée
- Prévision PV complète du jour avec surlignage des heures sélectionnées

**Endpoint** : `GET /api/leaf/optimize?day_offset={0|1|2}`

**Config Réglages** : toggle activation, capacité batterie (24 kWh par défaut pour ton Leaf ZE0), puissance charge (3.6 kW), SoC actuel/cible, fenêtre horaire préférée, allow_grid_import.

### 3. 📡 Export Grafana / InfluxDB

**Endpoint Prometheus `/metrics`** (format texte standard, activable par toggle) :
- `seh_uptime_seconds`, `seh_pv_power_watts`, `seh_load_power_watts`
- `seh_grid_power_watts` (positif = import, négatif = export)
- `seh_battery_power_watts`, `seh_battery_soc_percent`
- Par MPPT : `seh_mppt_pv_watts{mppt="1"}`, `seh_mppt_yield_today_kwh`
- Par MultiPlus : `seh_multiplus_ac_in_watts{multiplus="227"}`, `seh_multiplus_ac_out_watts`
- Par batterie : `seh_bms_soc_percent{group="JK",type="jkbms",bat="1"}`, voltage, current, power, temperature, cycles, soh
- Totaux jour : `seh_today_pv_kwh`, `seh_today_import_kwh`, etc.

**Publisher InfluxDB v2** (`backend/influx_publisher.py`) — module séparé :
- Push en line protocol HTTP (toutes les N secondes, défaut 30s)
- Measurements : `seh_system`, `seh_mppt`, `seh_multiplus`, `seh_bms`
- Tags : group, type, mppt, multiplus, bat
- Authentication Bearer Token (InfluxDB 2.x natif)
- Non bloquant : urlopen exécuté dans un thread pool
- Stats exposées via `/api/integrations/influx/status` (success/fail count, last error, last push ago)
- Bouton "Test push" dans les réglages pour vérifier la config avant de l'activer

**Endpoints** :
- `GET /metrics` (Prometheus)
- `GET /api/integrations/influx/status`
- `POST /api/integrations/influx/test`

**Section Réglages** : toggle Prometheus (défaut ON), toggle InfluxDB (OFF), URL/bucket/org/token, interval push.

## 📝 Fichiers modifiés/créés

| Fichier | Changements |
|---------|-------------|
| `backend/main.py` | +endpoints `/api/forecast/accuracy`, `/api/leaf/optimize`, `/metrics`, `/api/integrations/influx/{status,test}` ; persistence snapshot dans `/api/forecast` ; `influx_loop` task ; import `PlainTextResponse` ; global `_influx` + update au reload config |
| `backend/database.py` | +table `solar_forecast_snapshots` + index, +méthodes `save_forecast_snapshot`, `get_forecast_vs_actual`, `cleanup_old_forecasts` |
| `backend/config_manager.py` | +sections `leaf` et `integrations` dans DEFAULT_CONFIG |
| `backend/influx_publisher.py` | **NOUVEAU** : module InfluxDB v2 line protocol publisher |
| `backend/tests.py` | +17 tests Sprint 6 (snapshots, accuracy, Leaf, Prometheus, Influx, config) |
| `frontend/index.html` | +2 onglets (🔮 Prévisions, 🚗 Leaf), +pages complètes avec KPIs/graphiques Chart.js, +CSS (leaf-hero, leaf-soc-bar, leaf-plan, fc-quality), +JS `loadForecastAccuracy`, `renderForecastAccuracy`, `loadLeaf`, `renderLeaf`, `setLeafDay`, `testInflux`, +sections Réglages Leaf et Intégrations |

## 🧪 Tests

**40 tests** (+17 par rapport au sprint 5) — `pytest tests.py -v`

Nouveaux tests :
- **Forecast snapshots** (4) : save + retrieve, idempotence, accuracy vide, accuracy avec données
- **Leaf optimize** (3) : désactivé par défaut, needs_charge sans forecast, already_charged
- **Prometheus** (4) : basic metrics, Victron data inclus, BMS inclus, désactivable
- **Influx publisher** (4) : disabled par défaut, enabled requires URL+token, format lines basic, format lines avec BMS
- **Config v6** (2) : sections leaf et integrations présentes

## 🎯 Comment utiliser chaque feature

### Prévisions vs réel
1. Active les Prévisions solaires si pas déjà fait (Réglages → Solar forecast)
2. Consulte la page Prévisions maintenant — elle affichera "pas encore d'historique"
3. Attends 2-3 jours : chaque fetch de `/api/forecast` sauvegarde un snapshot automatiquement
4. Reviens sur l'onglet Prévisions → les graphiques et metrics apparaissent

### Leaf optimizer
1. Réglages → Nissan Leaf → active le module
2. Renseigne ton SoC actuel (met à jour après chaque utilisation)
3. Défini la fenêtre préférée (heures où tu es là pour brancher, ex 10h-17h le week-end)
4. Si tu veux un complément grid quand le PV manque (matin d'hiver), coche "Autoriser complément grid"
5. Consulte l'onglet Leaf → tabs Aujourd'hui/Demain/Après-demain

**Note** : C'est une **recommandation**, pas du pilotage automatique. Quand tu connecteras EVCC + OpenEVSE, les horaires pourront être poussés directement — mais pour l'instant c'est toi qui appliques manuellement.

### Prometheus / Grafana
1. Réglages → Intégrations → coche "Activer endpoint Prometheus"
2. Dans Grafana Agent / Prometheus scraper : target `http://ton-ip:8080/metrics`
3. Rafraîchissement recommandé : 30s
4. Exemple Prometheus job :
   ```yaml
   - job_name: smart_energy_hub
     scrape_interval: 30s
     static_configs:
       - targets: ['192.168.x.x:8080']
   ```

### InfluxDB v2
1. Réglages → Intégrations → active "Push InfluxDB"
2. URL, org, bucket, token (créés dans ton InfluxDB)
3. Interval par défaut 30s, descendable à 10s pour plus de résolution
4. Bouton "Test push" pour valider la config avant sauvegarde
5. Statut live via `GET /api/integrations/influx/status`

### Dashboard Grafana suggéré

Une fois InfluxDB configuré, import de ce dashboard Grafana :
- **Row 1** : Jauges SoC moyen, Puissance PV courante, Grid Import/Export
- **Row 2** : Graphique time-series 24h : PV + Load + Grid + Battery
- **Row 3** : Bar chart production PV par jour sur 30j
- **Row 4** : Heatmap SoC × heure pour identifier les patterns

Requêtes Flux InfluxDB exemples :
```flux
// Production PV instantanée
from(bucket: "solar")
  |> range(start: -15m)
  |> filter(fn: (r) => r._measurement == "seh_system" and r._field == "pv_power")

// SoC par batterie
from(bucket: "solar")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "seh_bms" and r._field == "soc")
  |> group(columns: ["group", "bat"])
```

## 🚫 Pas dans ce sprint

- **Pilotage automatique EVCC/OpenEVSE** : l'optimiseur donne une recommandation, l'exécution est manuelle. Quand EVCC sera branché, on pourra pousser la consigne via MQTT / HTTP
- **Contrôle onduleur** (reporté depuis sprint 4) : changer priorités source/charge nécessite toujours des tests hardware

## 📈 Bilan cumulé Sprint 5 + 6

- **+7 endpoints** API
- **+2 onglets** dans l'UI (Historique, Prévisions, Leaf → 3 pour 5+6)
- **+17 tests** → **40 tests** au total
- **+3 modules** (sections config) : roi, leaf, integrations
- **+2 modules dédiés** : `influx_publisher.py`, tests
- **+1 endpoint /health** + HEALTHCHECK Docker
- **4 bugs/régressions** évités grâce à la couverture de tests (agrégation import/export, inversion signe, migration DB, idempotence backfill)
