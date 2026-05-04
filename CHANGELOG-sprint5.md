# Sprint 5 — Fiabilité et données

## ✅ Livré

### 1. 📊 Widget "Aujourd'hui vs Hier" (Vue d'ensemble)

Apparaît automatiquement en haut de la Vue d'ensemble dès qu'il y a au moins 1 jour de données. Affiche pour aujourd'hui :
- Production PV, Conso, Import, Export, Autosuffisance, Économies
- Comparaison avec hier : flèche ↑↓, % de variation, valeur d'hier en sous-texte
- Code couleur intelligent : ↑ vert pour production/export/économies, ↑ rouge pour import/conso (un import en hausse n'est pas une bonne nouvelle)

Rafraîchi toutes les 10 minutes en tâche de fond.

**Endpoint** : `GET /api/today_vs_yesterday`

### 2. 📆 Heatmap annuelle de production PV (nouvel onglet "Historique")

Vue type GitHub : 365 jours × 7 lignes (jours de la semaine), une case par jour colorée selon le kWh produit (5 niveaux). Permet de :
- Repérer visuellement la saisonnalité
- Détecter les jours anormalement bas (poussière, ombre nouvelle, défaut MPPT)
- Voir d'un coup d'œil les périodes les plus productives

**Tooltip** au survol : date + détail (PV, conso, export du jour).
**KPIs en tête** : production totale, moyenne/jour, meilleur jour, export total.

**Endpoint** : `GET /api/heatmap/pv?days=365`

### 3. 🚨 Alertes Telegram enrichies

Quatre nouveaux types d'alertes, tous configurables et désactivables :

| Type | Déclencheur | Cooldown |
|------|-------------|----------|
| 📅 **Résumé hebdo** | Samedi soir à 20h (configurable) | Hebdo |
| ☁️ **PV anormalement bas** | En journée 8h-18h, si PV < X% de la moy. 7j même heure (X défaut 40%) | Cooldown standard |
| 📡 **Source silencieuse** | Aucune mise à jour Victron ou BMS depuis > N min (défaut 15) | Par source |
| 🔄 **Cycles batterie élevés** | Batterie atteint le seuil (défaut 4000 cycles) | 1 fois par batterie |

Chaque alerte a son propre toggle dans Réglages → ⚙️ Alertes avancées (collapsible).

### 4. 🏥 Santé batteries (intégré dans onglet Parc)

Nouvelle section affichée sous la grille des batteries du Parc :
- **Anneau SoH** circulaire animé pour chaque batterie (couleur selon niveau)
- **Status** : Excellent / Bon / Correct / Attention / Critique
- **Cycles utilisés** : nombre actuel, % du budget, restants estimés, années restantes
- **SoH** : reporté par le BMS (JK-BMS) ou estimé linéairement depuis cycles (Pylontech)
- **Référence cycles** : 6000 pour LiFePO4 (JK-BMS), 4500 pour Pylontech US2000B
- **Récap** en haut : SoH moyen, cycles moyens, batterie la plus dégradée

**Endpoint** : `GET /api/battery_health`

### 5. 🏥 Healthcheck Docker (`/health`)

Endpoint qui vérifie les composants critiques :
- **DB** : connexion + requête SELECT 1 OK
- **Polling** : au moins une source mise à jour < 5 min
- **Config** : ConfigManager initialisé

Retourne :
- **200 ok** : tout va bien
- **200 degraded** : polling stale mais DB+config OK
- **503 error** : DB ou config en erreur

Configuration Dockerfile :
```
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1
```

→ Docker / Proxmox peut détecter un freeze et redémarrer automatiquement.

### 6. 🧪 Suite de tests pytest (`backend/tests.py`)

**23 tests** couvrant :
- **Migration DB v1→v2** (3 tests) : colonnes kWh, idempotence, données préservées
- **Agrégation import/export** (3 tests) : régression sprint 4 alternances, pure import, pure export
- **Backfill historique** (3 tests) : ne touche que les jours nuls, idempotence, dry_run inoffensif
- **Configuration** (3 tests) : sections ROI, inversion signes, alertes enrichies
- **API Fleet** (2 tests) : vide, multi-groupes mixés
- **API ROI** (2 tests) : config vide, calcul avec données
- **API Today vs Yesterday** (2 tests) : vide, avec deltas
- **API Health** (2 tests) : ok, erreur sans DB
- **API Battery Health** (3 tests) : vide, estimation SoH Pylontech, grades de santé

Lancer : `pip install -r requirements-dev.txt && pytest tests.py -v`

Premier filet de sécurité contre les régressions futures.

## 📝 Fichiers modifiés/créés

| Fichier | Changements |
|---------|-------------|
| `backend/main.py` | +endpoints `/api/today_vs_yesterday`, `/api/heatmap/pv`, `/api/battery_health`, `/health`, mount StaticFiles wrappé pour les tests |
| `backend/alerts.py` | +4 types d'alertes, +résumé hebdo, +stockage historique PV par heure, +tracking source freshness |
| `backend/config_manager.py` | +clés alerts enrichies (pv_low_alert, source_stale_alert, cycles_alert, weekly_summary, etc.) |
| `backend/tests.py` | **NOUVEAU** : 23 tests pytest |
| `backend/requirements-dev.txt` | **NOUVEAU** : pytest + httpx |
| `Dockerfile` | +curl, +HEALTHCHECK |
| `frontend/index.html` | +onglet "Historique", +widget today/yesterday, +CSS heatmap+tvy+battery health, +JS (loadTodayVsYesterday, loadHistory, loadBatteryHealth, renderBatteryHealthInFleet), +section "Alertes avancées" collapsible dans réglages |

## 🚫 Reporté au sprint 6

- Comparaison prévision vs réel (graphique superposé)
- Optimiseur charge Leaf
- Export Grafana / InfluxDB

## 🧪 Vérifications effectuées

- 23/23 tests pytest passent ✅
- Compilation Python ✅
- Validation JS `node --check` ✅
- Test du healthcheck : 200 quand OK, 503 sans DB ✅
- Test endpoints Fleet/ROI/TvY/BatteryHealth avec données injectées ✅

## 💡 À surveiller en production

1. **PV low alert** : nécessite ~24-48h d'historique pour être pertinente (besoin de 24+ samples par tranche horaire)
2. **Source stale** : déclenche après 15 min (défaut). Ajustable si tu redémarres souvent ou si ton réseau a des microcoupures
3. **Cycles batterie** : seuil défaut 4000. Adapté à Pylontech (EoL ~4500). Pour JK-BMS LiFePO4 tu peux monter à 5000-5500
4. **Healthcheck** : si tu utilises Proxmox LXC sans Docker, l'endpoint `/health` reste utilisable manuellement ou via cron + script
