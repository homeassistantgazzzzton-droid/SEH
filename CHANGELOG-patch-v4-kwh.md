# Patch — Migration v4 : Backfill kWh historiques

## 🐛 Le bug (suite du patch v3)

Après le patch v3 (dédoublonnage), Alexis a constaté que ses jours antérieurs au sprint 4 affichaient :
- `pv_kwh` à 0 (aucune production prise en compte)
- ou des valeurs aberrantes (8000+ kWh par jour) issues d'un mauvais ordre de colonnes

**Cause racine** : Les colonnes `pv_kwh`, `load_kwh`, `import_kwh`, `export_kwh`, `bat_charge_kwh`, `bat_discharge_kwh` ont été ajoutées à `samples_5min` à partir du sprint 4 (migration v2). Toutes les données 5min enregistrées **avant** cette migration ont ces colonnes initialisées à 0. Conséquence : `SUM(pv_kwh)` au niveau hourly et daily renvoyait 0 pour ces jours-là.

Pire, un bug d'inversion de colonnes (probablement entre sprint 4 et 7) avait écrit `pv_avg` (W) dans `pv_kwh` des hourly → `SUM` cumulait des watts comme si c'étaient des kWh → 24 × ~350 W = 8400 kWh affichés.

## ✅ Fix : Migration v4 automatique

Pour chaque row `samples_5min` où `pv_kwh = 0` mais qu'il y a de l'activité (`pv_avg > 0` ou `load_avg > 0` ou `|grid_avg| > 0`), recalcule les colonnes kWh à partir des puissances moyennes :

```
kWh = puissance_moyenne_W × (5 min / 60) / 1000
```

Pour `import_kwh`/`export_kwh`/`bat_charge_kwh`/`bat_discharge_kwh`, on utilise `grid_avg` et `bat_avg` signés (approximation moins précise que le `CASE WHEN` sur `samples_raw`, qui ne sont plus disponibles pour les jours anciens, mais infiniment mieux que 0).

Une fois les 5min recalculés :
1. Reconstruction complète de `samples_hourly` depuis `samples_5min` (SUM des kWh par tranche d'heure pleine)
2. Reconstruction complète de `samples_daily` depuis `samples_hourly`

**Idempotente** grâce au marqueur `_seh_v4_kwh_backfilled` (ne se relance pas au prochain démarrage).

## 🔧 Endpoint manuel `/api/maintenance/repair_kwh`

Pour relancer manuellement le processus si besoin (par ex. après import d'une vieille DB) :

```bash
curl -X POST http://localhost:8080/api/maintenance/repair_kwh
```

Retourne :
```json
{
  "status": "ok",
  "5min_recalculated": 2087,
  "hourly_rebuilt": 190,
  "daily_recomputed": 9
}
```

## 🎨 Bouton UI

Nouveau bouton **🔧 Réparer kWh historiques** dans Réglages → 🔧 Maintenance, affiché en rouge pour signaler que c'est une action lourde mais sûre.

Au clic : confirmation → exécution → message de succès avec les compteurs → rafraîchissement automatique des vues (Today vs Yesterday, Rentabilité, Historique).

## 📝 Fichiers modifiés

| Fichier | Changements |
|---------|-------------|
| `backend/database.py` | +méthodes `_backfill_5min_kwh_columns`, `_rebuild_hourly_from_5min`, `repair_kwh_columns`, +migration v4 dans `_migrate` |
| `backend/main.py` | +endpoint `POST /api/maintenance/repair_kwh` |
| `backend/tests.py` | +4 tests pour migration v4 (backfill, idempotence, ne touche pas les valeurs correctes, repair_kwh manuel) |
| `frontend/index.html` | +bouton 🔧 Réparer kWh + handler `dbRepairKwh()` |

## 🧪 Tests : 56/56 ✅

+4 nouveaux tests dans `TestMigrationV4KwhBackfill` :
- `test_v4_backfills_zero_kwh_with_avg` : recalcule pv_kwh depuis pv_avg, vérifie hourly reconstruit
- `test_v4_idempotent` : 2e démarrage ne relance pas la migration
- `test_v4_does_not_touch_already_correct_kwh` : préserve les rows avec `pv_kwh > 0` déjà calculé
- `test_repair_kwh_columns_returns_counts` : endpoint manuel retourne les bons compteurs

## 🎯 Pour les nouveaux déploiements

Au premier démarrage tu verras dans les logs :
```
INFO database Migration v4: 2087 samples_5min recalculés. Reconstruction hourly et daily...
INFO database Migration v4: 190 hourly + 9 daily reconstruits
```

Aucune action manuelle requise — la migration s'applique toute seule. Si jamais tu veux la relancer (cas extrême), bouton 🔧 dans les réglages disponible.

## 💡 Récap des migrations DB cumulées

| Version | Apport |
|---------|--------|
| v1 | Schéma initial (sprint 1) |
| v2 | Ajout colonnes kWh dans `samples_5min` (sprint 4) |
| v3 | UNIQUE INDEX sur ts + dédoublonnage automatique (patch sprint 7) |
| **v4** | **Backfill rétroactif des kWh manquants/erronés depuis pv_avg/load_avg/grid_avg/bat_avg (ce patch)** |

La DB est désormais auto-cicatrisante : peu importe l'historique de versions par lequel elle est passée, elle se met à jour proprement au démarrage de chaque version récente.
